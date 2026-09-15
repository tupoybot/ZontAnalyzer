import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from recover_dashboard import run


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "cloud-work").mkdir()
        (self.root / "scope.json").write_text(json.dumps({"allowed_folder_id": "folder", "allowed_cloud_id": "cloud"}))
        (self.root / "cloud-work/inputs.tfvars.json").write_text('{"environment":"dev"}')
        (self.root / "deploy-token").write_text("test-token")
        (self.root / "deploy-apply.log").write_text("test fixture: CreateDashboardMetadata")
        self.dashboard = SimpleNamespace(id="dashboard", folder_id="folder", name="zont-dev-probe")

    def test_recovers_only_exact_scoped_match(self):
        with patch("recover_dashboard.request", return_value={"cloudId": "cloud"}), \
                patch("recover_dashboard.grpc.secure_channel"), patch("recover_dashboard.DashboardServiceStub") as stub:
            stub.return_value.List.return_value = SimpleNamespace(dashboards=[self.dashboard], next_page_token="")
            run(self.root)
            lookup = stub.return_value.List.call_args.args[0]
            self.assertEqual(lookup.folder_id, "folder")
            self.assertEqual(lookup.filter, 'name="zont-dev-probe"')
            self.assertEqual((self.root / "dashboard-id").read_text(), "dashboard")

    def test_other_failure_never_queries_cloud(self):
        (self.root / "deploy-apply.log").write_text("unrelated failure")
        with patch("recover_dashboard.request") as request, self.assertRaises(ValueError):
            run(self.root)
        request.assert_not_called()

    def test_wrong_cloud_never_queries_monitoring(self):
        with patch("recover_dashboard.request", return_value={"cloudId": "other"}), \
                patch("recover_dashboard.grpc.secure_channel") as channel, self.assertRaises(ValueError):
            run(self.root)
        channel.assert_not_called()

    def test_ambiguous_match_is_not_imported(self):
        with patch("recover_dashboard.request", return_value={"cloudId": "cloud"}), \
                patch("recover_dashboard.grpc.secure_channel"), patch("recover_dashboard.DashboardServiceStub") as stub:
            stub.return_value.List.return_value = SimpleNamespace(dashboards=[self.dashboard] * 2, next_page_token="")
            with self.assertRaises(ValueError):
                run(self.root)
        self.assertFalse((self.root / "dashboard-id").exists())


if __name__ == "__main__":
    unittest.main()
