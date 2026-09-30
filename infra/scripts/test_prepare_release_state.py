"""An existing timer is adopted without changing its identity or cadence."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from prepare_release_state import load_state, prepare


class ReleaseStateTest(unittest.TestCase):
    def setUp(self):
        self.inputs = {"folder_id": "folder", "cloud_id": "cloud", "timer_service_account_id": "timer",
                       "enable_monitoring_timer": True, "production_ydb_request_units_per_second": 100}
        self.state = {"resources": [
            {"mode": "managed", "type": kind, "name": name, "instances": [{"attributes": {"id": identifier}}]}
            for kind, name, identifier in (
                ("yandex_function_trigger", "monitoring", "monitoring"),
                ("yandex_serverless_container", "application", "application"),
                ("yandex_ydb_database_serverless", "production", "database"),
            )
        ]}
        self.timer = {"folderId": "folder", "source": {"timer": {"cronExpression": "*/15 * ? * * *"}},
                      "action": [{"invokeContainer": {"containerId": "application", "serviceAccountId": "timer",
                                                       "path": "/internal/monitoring"},
                                  "retryPolicy": {"retryAttempts": "1", "interval": "10s"}}]}
        self.database = {"folderId": "folder", "serverlessDatabase": {
            "enableThrottlingRcuLimit": True, "throttlingRcuLimit": "500", "provisionedRcuLimit": "0"}}

    def read(self, host, path):
        if host == "resource-manager.api.cloud.yandex.net":
            return {"cloudId": "cloud"}
        if host == "serverless-triggers.api.cloud.yandex.net":
            return self.timer
        if host == "ydb.api.cloud.yandex.net":
            return self.database
        raise AssertionError("unexpected service")

    def test_adopts_exact_existing_identity_and_preserves_release_capacity(self):
        result = prepare(self.inputs, self.state, self.read, preserve_runtime=True)
        self.assertEqual(result["monitoring_trigger_import_id"], "monitoring")
        self.assertEqual(result["monitoring_timer_schedule"], "*/15 * ? * * *")
        self.assertEqual(result["production_ydb_request_units_per_second"], 500)
        self.assertEqual(self.inputs["production_ydb_request_units_per_second"], 100)

    def test_explicit_infrastructure_change_keeps_requested_capacity(self):
        result = prepare(self.inputs, self.state, self.read)
        self.assertEqual(result["production_ydb_request_units_per_second"], 100)

    def test_already_adopted_timer_is_not_imported_again(self):
        self.state["resources"][0]["type"] = "yandex_serverless_triggers"
        result = prepare(self.inputs, self.state, self.read, preserve_runtime=True)
        self.assertNotIn("monitoring_trigger_import_id", result)
        self.assertEqual(result["monitoring_timer_schedule"], "*/15 * ? * * *")

    def test_empty_first_deploy_state_does_not_import_stale_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("")
            state = load_state(path)
            self.assertEqual(state, {"resources": []})
            result = prepare({**self.inputs, "monitoring_trigger_import_id": "stale"},
                             state, self.read, preserve_runtime=True)
            self.assertNotIn("monitoring_trigger_import_id", result)
            path.write_text(" ")
            with self.assertRaises(json.JSONDecodeError):
                load_state(path)

    def test_other_cloud_and_changed_trigger_target_fail_closed(self):
        inputs = {**self.inputs, "cloud_id": "other"}
        with self.assertRaisesRegex(ValueError, "authorized cloud"):
            prepare(inputs, self.state, self.read)
        for field in ("folderId",):
            original = copy.deepcopy(self.timer)
            self.timer[field] = "other"
            with self.assertRaisesRegex(ValueError, "unexpected configuration"):
                prepare(self.inputs, self.state, self.read)
            self.timer = original
        self.timer["action"][0]["invokeContainer"]["containerId"] = "other"
        with self.assertRaisesRegex(ValueError, "unexpected configuration"):
            prepare(self.inputs, self.state, self.read)

    def test_cross_folder_database_and_capacity_mode_fail_closed(self):
        self.database["folderId"] = "other"
        with self.assertRaisesRegex(ValueError, "authorized folder"):
            prepare(self.inputs, self.state, self.read, preserve_runtime=True)
        self.database["folderId"] = "folder"
        self.database["serverlessDatabase"]["provisionedRcuLimit"] = "1"
        with self.assertRaisesRegex(ValueError, "capacity mode"):
            prepare(self.inputs, self.state, self.read, preserve_runtime=True)


if __name__ == "__main__":
    unittest.main()
