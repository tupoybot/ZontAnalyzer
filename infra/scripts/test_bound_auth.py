import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bound_auth


class AuthLimitsTest(unittest.TestCase):
    def test_absent_identity_never_calls_cloud(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            (private / "cloud-work").mkdir()
            (private / "cloud-work/inputs.tfvars.json").write_text('{}')
            with patch.object(bound_auth, 'request') as request:
                bound_auth.run(private)
            request.assert_not_called()

    def test_foreign_function_is_not_mutated(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            (private / "cloud-work").mkdir()
            (private / "cloud-work/inputs.tfvars.json").write_text(json.dumps({
                "identity": {"client_id": "test"}, "folder_id": "expected"}))
            (private / "cloud-outputs.json").write_text(json.dumps({"auth_function_id": {"value": "function"}}))
            (private / "deploy-token").write_text("fixture")
            with (
                patch.object(bound_auth, 'request', return_value={"folderId": "foreign"}) as request,
                self.assertRaisesRegex(ValueError, 'scope'),
            ):
                bound_auth.run(private)
            self.assertEqual(request.call_count, 1)
