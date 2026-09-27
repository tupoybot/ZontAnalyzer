import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import grafana_m6_setup as g

ROOT = Path(__file__).parents[1] / "grafana"


class FakeAPI:
    def __init__(self, existing=None):
        self.existing = existing or {}
        self.calls = []

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method != "GET":
            return 200, {}
        return (200, self.existing[path]) if path in self.existing else (404, None)


class M6Tests(unittest.TestCase):
    def rules(self):
        return g.render(json.loads((ROOT / "m6-alert-rules.json").read_text())["rules"], "pilot")

    def test_new_resources_paused_and_only_owned_writes(self):
        api = FakeAPI()
        writes = g.prepare(api, ROOT, "prom-private", "pilot", "private-channel")
        self.assertTrue(all(method == "GET" for method, _, _ in api.calls))
        rules = [payload for _, path, payload in writes if "alert-rules" in path]
        self.assertEqual(len(rules), 14)
        for rule in rules:
            self.assertTrue(rule["isPaused"])
            self.assertEqual(rule["notification_settings"], {"receiver": "private-channel"})
            self.assertEqual(rule["data"][0]["datasourceUid"], "prom-private")
            self.assertNotIn("${environment}", json.dumps(rule))
            self.assertEqual(rule["folderUID"], "zont-m6")
        self.assertFalse(any("policies" in path or "contact-points" in path or "zont-m1" in path
                             for _, path, _ in writes))

    def test_rerun_preserves_active_rules_and_private_delivery(self):
        existing = {}
        for index, rule in enumerate(self.rules()):
            rule["isPaused"] = bool(index % 2)
            rule["notification_settings"] = {"receiver": "existing-channel", "group_wait": "45s"}
            existing[f'/api/v1/provisioning/alert-rules/{rule["uid"]}'] = rule
        writes = g.prepare(FakeAPI(existing), ROOT, "prom", "pilot", "different-channel")
        for method, path, payload in writes:
            if "alert-rules" in path:
                self.assertEqual(method, "PUT")
                self.assertEqual(payload["isPaused"], existing[path]["isPaused"])
                self.assertEqual(payload["notification_settings"], existing[path]["notification_settings"])

    def test_late_ownership_conflict_prevents_every_write(self):
        last = self.rules()[-1]
        for field, value in (("folderUID", "foreign"), ("title", "foreign"), ("isPaused", None),
                             ("labels", {"project": "foreign", "environment": "pilot"})):
            with self.subTest(field=field):
                rule = copy.deepcopy(last)
                rule[field] = value
                api = FakeAPI({f'/api/v1/provisioning/alert-rules/{rule["uid"]}': rule})
                with self.assertRaises(g.SetupError):
                    g.prepare(api, ROOT, "prom", "pilot", None)
                self.assertTrue(all(method == "GET" for method, _, _ in api.calls))

    def test_active_unrouted_rule_requires_private_contact(self):
        rule = self.rules()[0]
        rule["isPaused"] = False
        api = FakeAPI({f'/api/v1/provisioning/alert-rules/{rule["uid"]}': rule})
        with self.assertRaises(g.SetupError):
            g.prepare(api, ROOT, "prom", "pilot", None)

    def test_dashboard_conflict_refused(self):
        dashboard = json.loads((ROOT / "m6-dashboard.json").read_text())
        api = FakeAPI({f"/api/dashboards/uid/{g.DASHBOARD_UID}": {
            "dashboard": dashboard, "meta": {"folderUid": "foreign"},
        }})
        with self.assertRaises(g.SetupError):
            g.prepare(api, ROOT, "prom", "pilot", None)

    def test_bad_environment_rejected_before_network(self):
        for environment in ('pilot"} or vector(1)', "", None, "a/b"):
            with self.subTest(environment=environment), patch.object(g, "_load_json", return_value={
                "environment": environment,
            }), patch.object(g, "GrafanaAPI") as api:
                with self.assertRaises(g.SetupError):
                    g.run(Path("private.json"))
                api.assert_not_called()

    def test_tokens_exclude_cached_double_count_and_health_missing_is_alerting(self):
        rules = {rule["uid"].rsplit("-", 1)[-1]: rule for rule in self.rules()}
        self.assertIn('kind=~"input|output"', rules["tokens"]["data"][0]["model"]["expr"])
        for name in ("sync", "stale", "job", "fallback"):
            self.assertEqual(rules[name]["noDataState"], "Alerting")
        self.assertNotEqual(rules["sync"]["data"], rules["job"]["data"])

    def test_private_threshold_override_and_reserved_budget(self):
        writes = g.prepare(FakeAPI(), ROOT, "prom", "pilot", None, {"stale": 120000})
        rules = {payload["uid"].rsplit("-", 1)[-1]: payload
                 for _, path, payload in writes if "alert-rules" in path}
        self.assertEqual(rules["stale"]["data"][1]["model"]["conditions"][0]["evaluator"]["params"],
                         [120000])
        self.assertIn("zont_monthly_ai_reserved_tokens", rules["budget"]["data"][0]["model"]["expr"])

    def test_nonfinite_and_unknown_thresholds_rejected_before_network(self):
        for thresholds in ({"stale": float("nan")}, {"stale": float("inf")},
                           {"stale": True}, {"tokens": -1}, {"unknown": 1}):
            with self.subTest(thresholds=thresholds), patch.object(g, "_load_json", return_value={
                "environment": "pilot", "thresholds": thresholds,
            }), patch.object(g, "GrafanaAPI") as api:
                with self.assertRaises(g.SetupError):
                    g.run(Path("private.json"))
                api.assert_not_called()

    def test_explicit_activation_preserves_everything_except_pause(self):
        existing = {}
        rules = self.rules()
        for rule in rules:
            rule["notification_settings"] = {"receiver": "private-channel", "group_wait": "45s"}
            existing[f'/api/v1/provisioning/alert-rules/{rule["uid"]}'] = rule
        api = FakeAPI(existing)
        writes = g.activation_writes(api, ROOT, "pilot", ["monitoring", "snapshot"])
        self.assertEqual(len(writes), 2)
        self.assertTrue(all(method == "GET" for method, _, _ in api.calls))
        for method, path, rule in writes:
            self.assertEqual(method, "PUT")
            self.assertEqual(rule, {**existing[path], "isPaused": False})
        self.assertTrue(all(rule["isPaused"] for rule in existing.values()))

    def test_activation_refuses_unknown_or_unrouted_without_writes(self):
        for selected in (["unknown"], ["snapshot", "snapshot"], ["snapshot"]):
            api = FakeAPI()
            with self.assertRaises(g.SetupError):
                g.activation_writes(api, ROOT, "pilot", selected)
            self.assertTrue(all(method == "GET" for method, _, _ in api.calls))

    def test_latest_sample_queries_match_success_and_companion_labels(self):
        rules = {rule["uid"].removeprefix("zont-m6-pilot-"): rule for rule in self.rules()}
        for name in ("sync", "snapshot", "monitoring", "job"):
            expr = rules[name]["data"][0]["model"]["expr"]
            self.assertIn("_observed_timestamp_seconds", expr)
            self.assertIn("group_left max by", expr)
        self.assertIn('operation="monitoring"', rules["monitoring"]["data"][0]["model"]["expr"])
        self.assertIn("zont_telemetry_present", rules["stale"]["data"][0]["model"]["expr"])


if __name__ == "__main__":
    unittest.main()
