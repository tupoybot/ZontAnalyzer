import json
import unittest
from pathlib import Path

import grafana_setup as g

TEMPLATE_DIR = Path(__file__).parents[1] / "grafana"


class FakeAPI:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        result = self.responses.pop(0)
        return result


class GrafanaSetupTests(unittest.TestCase):
    def test_stack_url_validation(self):
        for url in (
            "http://stack.grafana.net",
            "https://grafana.net",
            "https://stack.example.net",
            "https://user:secret@stack.grafana.net",
            "https://stack.grafana.net:444",
            "https://stack.grafana.net/path",
            "https://stack.grafana.net?x=1",
            "https://stack.grafana.net#x",
        ):
            with self.subTest(url=url), self.assertRaises(g.SetupError):
                g.validate_stack_url(url)
        self.assertEqual(g.validate_stack_url("https://stack.grafana.net:443"), ("stack.grafana.net", 443))

    def test_datasource_must_be_unique_default_or_configured(self):
        api = FakeAPI([(
            200,
            [{"uid": "a", "type": "prometheus", "isDefault": True},
             {"uid": "b", "type": "prometheus", "isDefault": True}],
        )])
        with self.assertRaises(g.SetupError):
            g.discover_datasource(api, None)
        api = FakeAPI([(200, [{"uid": "a", "type": "prometheus"}, {"uid": "b", "type": "loki"}])])
        self.assertEqual(g.discover_datasource(api, "a"), "a")

    def test_conflicting_folder_is_refused_without_writes(self):
        api = FakeAPI([(200, {"uid": "zont-m1", "title": "Someone else's folder"})])
        with self.assertRaises(g.SetupError):
            g.ensure_folder(api)
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(api.calls[0][1], "/api/folders/zont-m1")

    def test_dashboard_update_preserves_version_and_selects_datasource(self):
        template = json.loads((TEMPLATE_DIR / "dashboard.json").read_text())
        existing = {
            "dashboard": {"uid": "zont-m1-overview", "title": template["title"], "tags": ["zont-m1"], "version": 8},
            "meta": {"folderUid": "zont-m1"},
        }
        api = FakeAPI([(200, existing), (200, {"status": "success"})])
        g.ensure_dashboard(api, template, "prom-42")
        payload = api.calls[-1][2]["dashboard"]
        self.assertEqual(payload["uid"], template["uid"])
        self.assertNotEqual(payload["uid"], api.calls[-1][2]["folderUid"])
        self.assertEqual(payload["version"], 8)
        self.assertEqual(payload["id"], None)
        self.assertEqual(payload["panels"][0]["datasource"]["uid"], "${datasource}")
        self.assertEqual(payload["templating"]["list"][0]["current"]["value"], "prom-42")
        self.assertEqual(api.calls[-1][2]["overwrite"], False)

    def test_rule_update_is_always_paused_and_never_touches_policy(self):
        template = json.loads((TEMPLATE_DIR / "alert-rule.json").read_text())
        existing = {
            "uid": "zont-m1-dev-health", "title": template["title"], "folderUID": "zont-m1",
            "labels": {"project": "zont-m1", "environment": "dev"},
        }
        api = FakeAPI([(200, existing), (200, {"status": "success"})])
        g.ensure_rule(api, template, "prom-42", "owner-channel")
        method, path, payload = api.calls[-1]
        self.assertEqual((method, path), ("PUT", "/api/v1/provisioning/alert-rules/zont-m1-dev-health"))
        self.assertTrue(payload["isPaused"])
        self.assertEqual(payload["notification_settings"], {"receiver": "owner-channel"})
        self.assertEqual(payload["data"][0]["datasourceUid"], "prom-42")
        self.assertNotIn("notification_policies", {call[1] for call in api.calls})

    def test_rule_conflict_refuses_update(self):
        template = json.loads((TEMPLATE_DIR / "alert-rule.json").read_text())
        api = FakeAPI([(
            200,
            {"uid": "zont-m1-dev-health", "title": "unrelated", "folderUID": "zont-m1",
             "labels": {"project": "zont-m1", "environment": "dev"}},
        )])
        with self.assertRaises(g.SetupError):
            g.ensure_rule(api, template, "prom-42", None)
        self.assertEqual(len(api.calls), 1)


if __name__ == "__main__":
    unittest.main()
