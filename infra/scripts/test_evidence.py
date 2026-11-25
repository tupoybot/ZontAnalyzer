import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import runtime_smoke
from archive_evidence import archive
from runtime_smoke import target


class EvidenceTest(unittest.TestCase):
    def test_healthy_probe_without_monium_receipt_fails_deployment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cloud-outputs.json").write_text(json.dumps({
                "probe_url": {"value": "https://probe.containers.yandexcloud.net"},
            }))
            (root / "deploy-token").write_text("fixture")
            responses = [(200, b"ok\n", None), (403, b"", None)]
            with (
                patch.object(runtime_smoke, "check", side_effect=responses),
                self.assertRaisesRegex(ValueError, "Monium"),
            ):
                runtime_smoke.run(root)
            self.assertTrue(json.loads((root / "runtime-smoke.json").read_text())["smoke_body_ok"])

    def test_archive_excludes_credentials_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "deploy-plan.log").write_text("evidence")
            (root / "deploy-token").write_text("secret")
            (root / "state.env").write_text("secret")
            (root / "xray-fixed.json").write_text("secret")
            (root / "cloud-outputs.json").symlink_to(root / "deploy-token")
            output = root / "result.tar.gz"
            archive(root, output)
            with tarfile.open(output) as bundle:
                self.assertEqual(bundle.getnames(), ["deploy-plan.log"])

    def test_runtime_token_only_goes_to_container_endpoint(self):
        self.assertEqual(target("https://probe.containers.yandexcloud.net/"), "probe.containers.yandexcloud.net")
        for url in ("http://probe.containers.yandexcloud.net/", "https://other.example/",
                    "https://probe.containers.yandexcloud.net.other.example/",
                    "https://user:pass@probe.containers.yandexcloud.net/",
                    "https://probe.containers.yandexcloud.net:8443/",
                    "https://probe.containers.yandexcloud.net/?redirect=other"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                target(url)


if __name__ == "__main__":
    unittest.main()
