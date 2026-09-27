"""Verify bounded deployment smoke in writable and maintenance modes without network calls."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import application_smoke


class ApplicationSmokeTest(unittest.TestCase):
    def check_mode(self, writes_enabled, identity=None, diagnostics_enabled=None):
        inputs = {"application_revision": "test-revision", "application_image": "test-image"}
        if writes_enabled is not None:
            inputs["application_writes_enabled"] = writes_enabled
        if identity:
            inputs["identity"] = identity
        enabled = writes_enabled is not False
        calls = []
        sequence = iter(["first", "second"])

        def invoke(host, path, authorization=None, body=None):
            calls.append((path, authorization, body))
            if host == "direct.example":
                return 403, b"{}"
            if authorization is None:
                return 401, b"{}"
            if path == "/ready":
                return 200, b"{}"
            if path == "/diagnostics":
                return 200, json.dumps({"revision": "test-revision", "writes_enabled":
                                       enabled if diagnostics_enabled is None else diagnostics_enabled}).encode()
            if path.startswith("/api/") and identity:
                return 401, b"{}"
            if not enabled:
                return 503, b'{"error":"maintenance"}'
            if body == {"invalid": True}:
                return 400, b"{}"
            return 200, json.dumps({"job_id": next(sequence), "result": {"quality": "valid"}}).encode()

        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            with patch.object(application_smoke, "context", return_value=(
                "gateway.example", "test-auth", inputs, {"application_url": {"value": "direct.example"}},
            )), patch.object(application_smoke, "invoke", side_effect=invoke), patch.object(
                application_smoke, "target", return_value="direct.example",
            ):
                application_smoke.run(private)
            result = json.loads((private / "application-smoke.json").read_text())
        self.assertEqual(result["writes_enabled"], enabled)
        self.assertEqual(result["external_api_requests"], 0)
        return result, calls

    def test_default_writable_smoke(self):
        result, _calls = self.check_mode(None)
        self.assertEqual(result["validation_http"], 400)
        self.assertNotEqual(result["first_job"], result["second_job"])

    def test_maintenance_smoke(self):
        result, calls = self.check_mode(False)
        self.assertEqual(result["maintenance_http"], 503)
        self.assertEqual(result["api_probe_http"], 503)
        self.assertEqual([path for path, auth, _ in calls if auth and path.startswith("/api/")],
                         ["/api/maintenance-smoke"])
        self.assertNotIn("first_job", result)

    def test_identity_maintenance_preserves_api_auth(self):
        result, _calls = self.check_mode(False, identity={"mode": "spa"})
        self.assertEqual(result["api_probe_http"], 401)

    def test_wrong_deployed_gate_fails_before_job(self):
        with self.assertRaises(AssertionError):
            self.check_mode(False, diagnostics_enabled=True)


if __name__ == "__main__":
    unittest.main()
