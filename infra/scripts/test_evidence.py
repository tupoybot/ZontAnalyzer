import tarfile
import tempfile
import unittest
from pathlib import Path

from archive_evidence import archive
from runtime_smoke import target


class EvidenceTest(unittest.TestCase):
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
