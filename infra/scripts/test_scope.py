"""Reject incomplete or cross-environment deployment inputs before authentication."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("scope", Path(__file__).with_name("check-scope.py"))
assert spec and spec.loader
scope_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope_module)


class ScopeTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.accounts = {"runtime_service_account_id": "runtime", "timer_service_account_id": "timer"}
        self.scope = {
            "allowed_cloud_id": "cloud", "allowed_folder_id": "folder",
            "allowed_state_bucket": "state",
            "allowed_environment_accounts": {"dev": self.accounts},
        }
        self.inputs = {"cloud_id": "cloud", "folder_id": "folder", "environment": "dev", **self.accounts}
        self.backend = {
            "bucket": "state", "key": "dev/terraform.tfstate", "use_lockfile": True,
            "endpoints": {"s3": "https://storage.yandexcloud.net"},
        }

    def check(self, inputs=None):
        values = [self.scope, self.inputs if inputs is None else inputs, self.backend]
        paths = []
        for index, value in enumerate(values):
            path = Path(self.directory.name) / f"{index}.json"
            path.write_text(json.dumps(value))
            paths.append(path)
        scope_module.check(*paths)

    def test_valid_inputs_and_no_change_plan(self):
        self.check()
        self.check({"format_version": "1.2", "variables": {
            key: {"value": value} for key, value in self.inputs.items()
        }})

    def test_missing_and_wrong_scope_values(self):
        for key in self.inputs:
            for replacement in (None, "other"):
                with self.subTest(key=key, replacement=replacement):
                    inputs = dict(self.inputs)
                    if replacement is None:
                        del inputs[key]
                    else:
                        inputs[key] = replacement
                    with self.assertRaises(ValueError):
                        self.check(inputs)

    def test_backend_rejections(self):
        original = dict(self.backend)
        for key, value in (
            ("bucket", "other"), ("key", "pilot/terraform.tfstate"),
            ("use_lockfile", False), ("endpoints", {"s3": "https://other.example"}),
        ):
            with self.subTest(key=key):
                self.backend = {**original, key: value}
                with self.assertRaises(ValueError):
                    self.check()

    def test_empty_scope_is_rejected(self):
        self.scope = {}
        with self.assertRaises(ValueError):
            self.check()


if __name__ == "__main__":
    unittest.main()
