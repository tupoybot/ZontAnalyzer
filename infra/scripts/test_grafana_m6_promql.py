#!/usr/bin/env python3
"""Generate promtool fixtures from the actual M6 templates into an isolated directory.

Run inside the tools container, then promtool test rules on the generated tests.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "grafana"


def generate(output: Path) -> None:
    rules = json.loads((ROOT / "m6-alert-rules.json").read_text())["rules"]
    expressions = {
        rule["uid"].removeprefix("zont-m6-${environment}-"):
        rule["data"][0]["model"]["expr"].replace("${environment}", "pilot")
        for rule in rules
    }
    checks = [{"record": "m6_fixture_" + name.replace("-", "_"), "expr": expression}
              for name, expression in expressions.items()]
    dashboard = json.loads((ROOT / "m6-dashboard.json").read_text())
    checks.extend({"record": f'm6_panel_{panel["id"]}',
                   "expr": target["expr"].replace("$environment", "pilot")}
                  for panel in dashboard["panels"] for target in panel.get("targets", []))
    output.mkdir(parents=True, exist_ok=True)
    (output / "rules.json").write_text(json.dumps({"groups": [{"name": "m6", "rules": checks}]}))
    tests = []
    for name, prefix, operation in (("snapshot", "zont_snapshot", ""),
                                  ("monitoring", "zont_cloud_job", ',operation="monitoring"'),
                                  ("sync", "zont_sync", "")):
        old = '{environment="pilot",instance="old"' + operation + '}'
        new = '{environment="pilot",instance="new"' + operation + '}'
        series = [
            {"series": prefix + "_success" + old, "values": "0 0 0"},
            {"series": prefix + "_observed_timestamp_seconds" + old, "values": "100 100 100"},
            {"series": prefix + "_success" + new, "values": "_ 1 0"},
            {"series": prefix + "_observed_timestamp_seconds" + new, "values": "_ 200 300"},
        ]
        tests.append({"name": name + " recovers across instances and detects next failure", "interval": "1m",
                      "input_series": series, "promql_expr_test": [
                          {"expr": expressions[name], "eval_time": time,
                           "exp_samples": [{"labels": "{}", "value": value}]}
                          for time, value in (("0m", 1), ("1m", 0), ("2m", 1))]})
        tests.append({"name": name + " missing data", "interval": "1m", "input_series": [],
                      "promql_expr_test": [{"expr": expressions[name], "eval_time": "1m",
                                            "exp_samples": [{"labels": "{}", "value": 1}]}]})
    tests.append({"name": "counter baseline retains cold-start error", "interval": "1m", "input_series": [
        {"series": 'zont_ydb_errors_total{environment="pilot",instance="cold"}', "values": "0 1"}],
        "promql_expr_test": [{"expr": '(' + expressions["ydb"] + ') > bool 0', "eval_time": "1m",
                              "exp_samples": [{"labels": "{}", "value": 1}]}]})
    tests.append({"name": "other successful operation cannot replace monitoring heartbeat", "interval": "1m",
                  "input_series": [
                      {"series": 'zont_cloud_job_success{environment="pilot",operation="report"}', "values": "1"},
                      {"series": 'zont_cloud_job_observed_timestamp_seconds{environment="pilot",operation="report"}',
                       "values": "100"}], "promql_expr_test": [{"expr": expressions["monitoring"],
                       "eval_time": "0m", "exp_samples": [{"labels": "{}", "value": 1}]}]})
    tests.append({"name": "proxy readiness recovers across instances without error counters", "interval": "1m",
                  "input_series": [
                      {"series": 'zont_proxy_ready{environment="pilot",instance="old"}', "values": "0 0"},
                      {"series": 'zont_proxy_ready{environment="pilot",instance="new"}', "values": "_ 1"},
                      {"series": 'zont_cloud_job_observed_timestamp_seconds'
                                 '{environment="pilot",instance="old",operation="monitoring"}',
                       "values": "100 100"},
                      {"series": 'zont_cloud_job_observed_timestamp_seconds'
                                 '{environment="pilot",instance="new",operation="monitoring"}',
                       "values": "_ 200"}], "promql_expr_test": [
                           {"expr": expressions["proxy"], "eval_time": time,
                            "exp_samples": [{"labels": "{}", "value": value}]}
                           for time, value in (("0m", 1), ("1m", 0))]})
    model_series = []
    for instance, enabled, observed, due in (("old", "1 1 1 1", "100 100 100 100", "0 0 0 0"),
                                           ("new", "_ 0 1 1", "_ 200 300 400", "_ 0 0 600")):
        labels = '{environment="pilot",instance="' + instance + '"}'
        for metric, values in (("zont_model_review_enabled", enabled),
                               ("zont_snapshot_observed_timestamp_seconds", observed),
                               ("zont_model_review_next_due_timestamp_seconds", due)):
            model_series.append({"series": metric + labels, "values": values})
    tests.append({"name": "model disabled on newest boot then enabled and deadline changed", "interval": "1m",
                  "input_series": model_series, "promql_expr_test": [
                      {"expr": expressions["model-overdue"], "eval_time": time,
                       "exp_samples": [] if value is None else [{"labels": "{}", "value": value}]}
                      for time, value in (("0m", 0), ("1m", None), ("2m", 120), ("3m", -420))]})
    tests.append({"name": "model missing state belongs to snapshot alert", "interval": "1m", "input_series": [],
                  "promql_expr_test": [{"expr": expressions["model-overdue"], "eval_time": "1m",
                                        "exp_samples": []}]})
    (output / "tests.json").write_text(json.dumps({"rule_files": ["rules.json"], "evaluation_interval": "1m",
                                                 "tests": tests}, indent=2))


if __name__ == "__main__":
    generate(Path(sys.argv[1]))
