#!/usr/bin/env python3
"""Generate promtool fixtures from the actual M6 templates into an isolated directory.

Run inside the tools container, then promtool test rules on the generated tests.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "grafana"


def panel_expression(expression: str) -> str:
    return expression.replace("$environment", "pilot").replace("$__interval", "1m").replace("$__range", "1h")


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
    checks.extend({"record": f'm6_panel_{panel["id"]}_{target["refId"]}',
                   "expr": panel_expression(target["expr"])}
                  for panel in dashboard["panels"] for target in panel.get("targets", []))
    output.mkdir(parents=True, exist_ok=True)
    (output / "rules.json").write_text(json.dumps({"groups": [{"name": "m6", "rules": checks}]}))
    tests = []
    counter_metrics = {
        7: ("zont_openai_calls_total", "zont_openai_failures_total"),
        9: ("zont_ydb_errors_total", "zont_publication_failures_total"),
        11: ("zont_proxy_failures_total", "zont_upstream_failures_total"),
        12: ("zont_egress_denials_total", "zont_egress_limit_total"),
    }
    counter_series = []
    counter_checks = []
    for panel_id, metrics in counter_metrics.items():
        panel = next(panel for panel in dashboard["panels"] if panel["id"] == panel_id)
        for index, metric in enumerate(metrics, start=1):
            labels = '{environment="pilot",instance="same"' + (',destination="openai"}' if panel_id == 11 else '}')
            counter_series.append({"series": metric + labels, "values": f"0+{index}x60"})
            counter_series.append({"series": metric + '{environment="dev",instance="same"}',
                                   "values": "0+99x60"})
            counter_checks.append({
                "expr": panel_expression(panel["targets"][index - 1]["expr"]), "eval_time": "1h",
                "exp_samples": [{"labels": '{destination="openai"}' if panel_id == 11 else '{}',
                                 "value": index * 60}],
            })
    tests.append({"name": "counter panels distinguish simultaneous metric names with otherwise identical labels",
                  "interval": "1m", "input_series": counter_series, "promql_expr_test": counter_checks})
    duration = next(panel for panel in dashboard["panels"] if panel["id"] == 4)
    duration_queries = {target["refId"]: target["expr"].replace("$environment", "pilot").replace("$__interval", "1m")
                        for target in duration["targets"]}
    tests.append({
        "name": "duration buckets group instances without carrying old samples into empty intervals",
        "interval": "1m",
        "input_series": [
            {"series": 'zont_cloud_job_duration_seconds{environment="pilot",instance="old",operation="maintenance"}',
             "values": "140 _ _"},
            {"series": 'zont_cloud_job_duration_seconds{environment="pilot",instance="new",operation="maintenance"}',
             "values": "_ 5 _"},
            {"series": 'zont_cloud_job_duration_seconds{environment="pilot",instance="tie",operation="maintenance"}',
             "values": "_ 4 _"},
            {"series": 'zont_cloud_job_duration_seconds{environment="pilot",instance="other",operation="scheduler"}',
             "values": "_ 12 _"},
            {"series": 'zont_cloud_job_duration_seconds{environment="pilot",instance="unlabelled"}',
             "values": "999 999 999"},
            {"series": 'zont_cloud_job_duration_seconds{environment="dev",operation="maintenance"}',
             "values": "999 999 999"},
            *[{"series": f'zont_{kind}_duration_seconds{{environment="pilot",instance="{instance}"}}',
               "values": values}
              for kind in ("analysis", "report")
              for instance, values in (("old", "100 _ _"), ("new", "_ 2 _"))],
        ],
        "promql_expr_test": [
            {"expr": duration_queries["A"], "eval_time": "1m", "exp_samples": [
                {"labels": '{operation="maintenance"}', "value": 5},
                {"labels": '{operation="scheduler"}', "value": 12},
            ]},
            *[{"expr": duration_queries[ref], "eval_time": "1m",
               "exp_samples": [{"labels": "{}", "value": 2}]} for ref in ("B", "C")],
            *[{"expr": expression, "eval_time": "2m", "exp_samples": []}
              for expression in duration_queries.values()],
            {"expr": duration_queries["A"].replace("[1m]", "[5m]"), "eval_time": "2m", "exp_samples": [
                {"labels": '{operation="maintenance"}', "value": 140},
                {"labels": '{operation="scheduler"}', "value": 12},
            ]},
            *[{"expr": duration_queries[ref].replace("[1m]", "[5m]"), "eval_time": "2m",
               "exp_samples": [{"labels": "{}", "value": 100}]} for ref in ("B", "C")],
        ],
    })
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
    proxy_panel = next(panel for panel in dashboard["panels"] if panel["id"] == 30)
    proxy_query = proxy_panel["targets"][0]["expr"].replace("$environment", "pilot").replace("$__interval", "1m")
    tests.append({
        "name": "proxy panel groups instances, retains failure and distinguishes missing measurements",
        "interval": "30s",
        "input_series": [
            {"series": 'zont_proxy_ready{environment="pilot",instance="old"}', "values": "1 0 _ _ 1 _ _"},
            {"series": 'zont_proxy_ready{environment="pilot",instance="new"}', "values": "_ _ 1 _ _ _ _"},
            {"series": 'zont_proxy_ready{environment="dev",instance="other"}', "values": "0 0 0 0 0 0 0"},
        ],
        "promql_expr_test": [
            {"expr": proxy_query, "eval_time": time,
             "exp_samples": [{"labels": "{}", "value": value}]}
            for time, value in (("0m", 1), ("1m", 0), ("2m", 1), ("3m", -1))
        ] + [
            {"expr": proxy_query.replace("[1m]", "[5m]"), "eval_time": "3m",
             "exp_samples": [{"labels": "{}", "value": 0}]},
        ],
    })
    tests.append({"name": "proxy panel explicitly marks an empty environment as unknown",
                  "input_series": [], "promql_expr_test": [{
                      "expr": proxy_query, "eval_time": "1m",
                      "exp_samples": [{"labels": "{}", "value": -1}],
                  }]})
    for panel_id, metric, labels, inverted in (
        (1, "zont_sync_success", "", False),
        (13, "zont_direct_fallback_allowed", "", True),
        (18, "zont_model_review_error", "", True),
        (22, "zont_snapshot_success", "", False),
        (28, "zont_telemetry_present", "", False),
        (29, "zont_cloud_job_success", ',operation="monitoring"', False),
    ):
        panel = next(panel for panel in dashboard["panels"] if panel["id"] == panel_id)
        query = panel_expression(panel["targets"][0]["expr"])
        healthy, failed = (0, 1) if inverted else (1, 0)
        tests.append({
            "name": f"status panel {panel_id} retains failure, recovers and leaves missing intervals unknown",
            "interval": "30s",
            "input_series": [
                {"series": metric + '{environment="pilot",instance="old"' + labels + '}',
                 "values": f"{healthy} {failed} _ _ {healthy} _ _"},
                {"series": metric + '{environment="pilot",instance="new"' + labels + '}',
                 "values": f"_ _ {healthy} _ _ _ _"},
                {"series": metric + '{environment="dev"' + labels + '}',
                 "values": f"{failed}+0x6"},
            ],
            "promql_expr_test": [
                {"expr": query, "eval_time": time,
                 "exp_samples": [{"labels": "{}", "value": value}]}
                for time, value in (("0m", healthy), ("1m", failed), ("2m", healthy), ("3m", -1))
            ] + [{"expr": query.replace("[1m]", "[5m]"), "eval_time": "3m",
                  "exp_samples": [{"labels": "{}", "value": failed}]}],
        })
    jobs = next(panel for panel in dashboard["panels"] if panel["id"] == 14)
    job_query = panel_expression(jobs["targets"][0]["expr"])
    tests.append({
        "name": "job status rows group instances, exclude unlabelled totals and preserve empty operation intervals",
        "interval": "30s",
        "input_series": [
            {"series": 'zont_cloud_job_success{environment="pilot",operation="scheduler",instance="old"}',
             "values": "1 0 _ _ 1 _ _"},
            {"series": 'zont_cloud_job_success{environment="pilot",operation="scheduler",instance="new"}',
             "values": "_ _ 1 _ _ _ _"},
            {"series": 'zont_cloud_job_success{environment="pilot",operation="monitoring"}',
             "values": "1 1 1 1 1 1 1"},
            {"series": 'zont_cloud_job_success{environment="pilot"}', "values": "0 0 0 0 0 0 0"},
        ],
        "promql_expr_test": [
            {"expr": job_query, "eval_time": time, "exp_samples": [
                {"labels": '{operation="scheduler"}', "value": value},
                {"labels": '{operation="monitoring"}', "value": 1},
            ]} for time, value in (("1m", 0), ("2m", 1), ("3m", -1))
        ],
    })
    for panel_id, metric, multiplier in (
        (16, "zont_model_review_last_success_timestamp_seconds", 1000),
        (17, "zont_model_review_attempts", 1),
        (20, "zont_monthly_ai_tokens", 1),
        (21, "zont_monthly_ai_token_budget", 1),
        (23, "zont_monthly_ai_reserved_tokens", 1),
    ):
        panel = next(panel for panel in dashboard["panels"] if panel["id"] == panel_id)
        query = panel_expression(panel["targets"][0]["expr"])
        tests.append({
            "name": f"snapshot card {panel_id} selects latest state, preserves zero and excludes failed snapshots",
            "interval": "1m",
            "input_series": [
                {"series": metric + '{environment="pilot",instance="old"}', "values": "500 500 500 500"},
                {"series": metric + '{environment="pilot",instance="new"}', "values": "_ 5 0 999"},
                {"series": 'zont_snapshot_observed_timestamp_seconds{environment="pilot",instance="old"}',
                 "values": "100 100 100 100"},
                {"series": 'zont_snapshot_observed_timestamp_seconds{environment="pilot",instance="new"}',
                 "values": "_ 200 300 400"},
                {"series": 'zont_snapshot_success{environment="pilot",instance="old"}', "values": "1 1 1 1"},
                {"series": 'zont_snapshot_success{environment="pilot",instance="new"}', "values": "_ 1 1 0"},
            ],
            "promql_expr_test": [
                {"expr": query, "eval_time": time,
                 "exp_samples": [] if value is None or (panel_id == 16 and value == 0)
                 else [{"labels": "{}", "value": value * multiplier}]}
                for time, value in (("0m", 500), ("1m", 5), ("2m", 0), ("3m", None), ("124m", None))
            ],
        })
    proposals_panel = next(panel for panel in dashboard["panels"] if panel["id"] == 19)
    proposals_query = panel_expression(proposals_panel["targets"][0]["expr"])
    tests.append({
        "name": "proposal counts use latest successful snapshot by status without summing copies",
        "interval": "1m",
        "input_series": [
            *[{"series": 'zont_model_review_proposals{environment="pilot",instance="' + instance
                         + '",status="' + status + '"}', "values": values}
              for instance, status, values in (
                  ("old", "open", "5 5 5"), ("old", "accepted", "2 2 2"),
                  ("new", "open", "_ 1 0"), ("new", "accepted", "_ 3 3"))],
            {"series": 'zont_snapshot_observed_timestamp_seconds{environment="pilot",instance="old"}',
             "values": "100 100 100"},
            {"series": 'zont_snapshot_observed_timestamp_seconds{environment="pilot",instance="new"}',
             "values": "_ 200 300"},
            {"series": 'zont_snapshot_success{environment="pilot",instance="old"}', "values": "1 1 1"},
            {"series": 'zont_snapshot_success{environment="pilot",instance="new"}', "values": "_ 1 0"},
        ],
        "promql_expr_test": [
            {"expr": proposals_query, "eval_time": "1m", "exp_samples": [
                {"labels": '{status="open"}', "value": 1},
                {"labels": '{status="accepted"}', "value": 3},
            ]},
            {"expr": proposals_query, "eval_time": "2m", "exp_samples": []},
        ],
    })
    report_panel = next(panel for panel in dashboard["panels"] if panel["id"] == 27)
    report_query = panel_expression(report_panel["targets"][0]["expr"])
    tests.append({
        "name": "report card chooses latest generation date across instances in milliseconds",
        "interval": "1m",
        "input_series": [
            {"series": 'zont_report_generated_timestamp_seconds{environment="pilot",instance="old"}',
             "values": "100 100"},
            {"series": 'zont_report_generated_timestamp_seconds{environment="pilot",instance="new"}',
             "values": "_ 200"},
        ],
        "promql_expr_test": [
            {"expr": report_query, "eval_time": "1m", "exp_samples": [{"labels": "{}", "value": 200000}]},
            {"expr": report_query, "eval_time": "62m", "exp_samples": []},
        ],
    })
    evidence_panel = next(panel for panel in dashboard["panels"] if panel["id"] == 26)
    for target in evidence_panel["targets"]:
        query = panel_expression(target["expr"])
        # Use the actual template selector so all six rows exercise their own component and quality.
        selector = query[query.index("zont_reliability_evidence{"):query.index("}[1m]") + 1]
        unhealthy = 0 if 'quality="data_fresh"' in selector else 1
        healthy = 1 - unhealthy
        tests.append({
            "name": f'evidence row {target["refId"]} groups copies without losing its quality semantics',
            "interval": "30s",
            "input_series": [
                {"series": selector[:-1] + ',instance="old"}', "values": f"{healthy} {unhealthy} _ _ {healthy} _ _"},
                {"series": selector[:-1] + ',instance="new"}', "values": f"_ _ {healthy} _ _ _ _"},
            ],
            "promql_expr_test": [
                {"expr": query, "eval_time": time, "exp_samples": [{"labels": "{}", "value": value}]}
                for time, value in (("1m", unhealthy), ("2m", healthy), ("3m", -1))
            ],
        })
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
