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

    def test_old_environment_inputs_cannot_delete_deployed_identity(self):
        plan = {"format_version": "1.2", "variables": {
            key: {"value": value} for key, value in self.inputs.items()
        }, "resource_changes": [{"address": "yandex_function.auth[0]",
                                 "change": {"actions": ["delete"]}}]}
        with self.assertRaisesRegex(ValueError, "removing deployed identity"):
            self.check(plan)

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

    def enable_identity(self):
        identity = {
            "client_id": "client", "issuer": "https://issuer.example/",
            "auth_service_account": "auth", "client_secret_id": "client-secret",
            "client_secret_version": "client-version", "transaction_secret_id": "transaction-secret",
            "transaction_version": "transaction-version",
        }
        self.scope["allowed_environment_identity"] = {"dev": identity.copy()}
        self.inputs["identity"] = {**identity, "code_sha256": "a" * 64}

    def test_identity_disabled_preserves_existing_scope(self):
        self.inputs["identity"] = None
        self.check()

    def test_configured_identity_cannot_be_omitted(self):
        self.enable_identity()
        self.inputs["identity"] = None
        with self.assertRaisesRegex(ValueError, "silently disabled"):
            self.check()
        del self.inputs["identity"]
        with self.assertRaisesRegex(ValueError, "silently disabled"):
            self.check()

    def test_allowlisted_identity_and_plan_are_accepted(self):
        self.enable_identity()
        self.check()
        self.check({"format_version": "1.2", "variables": {
            key: {"value": value} for key, value in self.inputs.items()
        }})

    def test_every_identity_reference_must_be_explicitly_allowlisted(self):
        self.enable_identity()
        original = self.inputs["identity"].copy()
        for key in self.scope["allowed_environment_identity"]["dev"]:
            for replacement in (None, "other", "", [original[key]]):
                with self.subTest(key=key, replacement=replacement):
                    self.inputs["identity"] = {**original, key: replacement}
                    with self.assertRaises(ValueError):
                        self.check()
            self.inputs["identity"] = original.copy()
            del self.inputs["identity"][key]
            with self.assertRaises(ValueError):
                self.check()

    def test_identity_cannot_use_another_environment_allowlist(self):
        self.enable_identity()
        self.scope["allowed_environment_identity"]["pilot"] = self.scope["allowed_environment_identity"].pop("dev")
        with self.assertRaises(ValueError):
            self.check()

    def test_identity_missing_allowlist_and_extra_resources_are_rejected(self):
        self.enable_identity()
        self.inputs["identity"]["folder_id"] = "other-folder"
        with self.assertRaises(ValueError):
            self.check()
        del self.inputs["identity"]["folder_id"]
        del self.scope["allowed_environment_identity"]
        with self.assertRaises(ValueError):
            self.check()

    def test_allowlist_cannot_reuse_existing_accounts_or_combine_secrets(self):
        for key, value in (("auth_service_account", "runtime"), ("auth_service_account", "timer"),
                           ("transaction_secret_id", "client-secret")):
            with self.subTest(key=key, value=value):
                self.enable_identity()
                self.inputs["identity"][key] = value
                self.scope["allowed_environment_identity"]["dev"][key] = value
                with self.assertRaises(ValueError):
                    self.check()

    def test_identity_artifact_and_shape_validation(self):
        for digest in (None, "latest", "a" * 63, "a" * 65, "g" * 64, "a" * 64 + "\n"):
            with self.subTest(digest=digest):
                self.enable_identity()
                self.inputs["identity"]["code_sha256"] = digest
                with self.assertRaises(ValueError):
                    self.check()
        for identity in (False, "identity", []):
            self.inputs["identity"] = identity
            with self.assertRaises(ValueError):
                self.check()


if __name__ == "__main__":
    unittest.main()
