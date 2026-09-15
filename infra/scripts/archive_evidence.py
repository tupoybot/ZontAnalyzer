"""Archive a fixed allowlist of private deployment evidence to the encrypted state bucket."""
import json
import sys
import tarfile
import tempfile
from pathlib import Path

import boto3

FILES = (
    "deploy-init.log", "deploy-plan.log", "deploy-plan.json", "deploy-apply.log",
    "cloud-outputs.json", "scaling.log", "scaling-refresh.log", "bounded-revision.json",
    "scaling-operation.json", "scaling-error.json", "runtime-smoke.json", "runtime-smoke.log",
    "application-source-inspect.json", "application-copy.log", "dashboard-recovery.log",
)


def archive(private, output):
    with tarfile.open(output, "w:gz") as bundle:
        for name in FILES:
            path = private / name
            if path.is_file() and not path.is_symlink():
                bundle.add(path, arcname=name, recursive=False)


def run(private, run_id):
    if not run_id.replace("-", "").isdigit():
        raise ValueError("numeric workflow run and attempt required")
    scope = json.loads((private / "scope.json").read_text())
    inputs = json.loads((private / "cloud-work/inputs.tfvars.json").read_text())
    if inputs["environment"] not in ("dev", "pilot"):
        raise ValueError("isolated environment required")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "evidence.tar.gz"
        archive(private, path)
        client = boto3.client("s3", endpoint_url="https://storage.yandexcloud.net", region_name="ru-central1")
        key = f"evidence/{inputs['environment']}/{run_id}/evidence.tar.gz"
        client.upload_file(str(path), scope["allowed_state_bucket"], key)
        head = client.head_object(Bucket=scope["allowed_state_bucket"], Key=key)
        if head.get("ServerSideEncryption") != "aws:kms" or not head.get("VersionId"):
            raise ValueError("evidence encryption or versioning not confirmed")
    print("Deployment evidence archived privately with encryption and versioning")


if __name__ == "__main__":
    run(Path(sys.argv[1]), sys.argv[2])
