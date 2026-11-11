import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("terraform_plan_retry.sh")


class TerraformPlanRetryTest(unittest.TestCase):
    def run_wrapper(self, command: str, attempts: int = 3) -> tuple[subprocess.CompletedProcess[str], str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            counter = root / "counter"
            fake = root / "fake-plan"
            fake.write_text(
                "#!/bin/sh\n"
                f"counter={counter!s}\n"
                "attempt=$(($(cat \"$counter\" 2>/dev/null || echo 0) + 1))\n"
                "printf '%s' \"$attempt\" > \"$counter\"\n"
                f"{command}\n"
            )
            fake.chmod(0o755)
            log = root / "plan.log"
            result = subprocess.run(
                [str(SCRIPT), str(log), str(attempts), str(fake)],
                text=True,
                capture_output=True,
                env={**os.environ, "TERRAFORM_PLAN_RETRY_DELAY_SECONDS": "0"},
                check=False,
            )
            return result, log.read_text()

    def test_retries_unavailable_provider_and_keeps_failed_evidence(self) -> None:
        result, log = self.run_wrapper(
            "if [ \"$attempt\" -lt 3 ]; then echo 'rpc error: code = Unavailable' >&2; exit 1; fi; echo complete"
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(log.count("code = Unavailable"), 2)
        self.assertIn("Terraform plan attempt 2 of 3 failed.", log)
        self.assertTrue(log.endswith("complete\n"))

    def test_does_not_retry_permanent_error(self) -> None:
        result, log = self.run_wrapper("echo 'permission denied' >&2; exit 1")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(log.count("permission denied"), 1)

    def test_returns_failure_after_retry_limit(self) -> None:
        result, log = self.run_wrapper("echo 'Unavailable' >&2; exit 7", attempts=2)

        self.assertEqual(result.returncode, 7)
        self.assertEqual(log.count("Unavailable"), 2)


if __name__ == "__main__":
    unittest.main()
