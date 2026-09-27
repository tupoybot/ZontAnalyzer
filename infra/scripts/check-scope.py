"""Validate private inputs before any credential acquisition or Terraform call."""
import json
import re
import sys
from pathlib import Path


def check(scope_path, inputs_path, backend_path=None):
    scope = json.loads(Path(scope_path).read_text())
    inputs = json.loads(Path(inputs_path).read_text())
    if "format_version" in inputs and "variables" in inputs:
        inputs = {key: value["value"] for key, value in inputs["variables"].items()}
    allowed_cloud = scope.get("allowed_cloud_id")
    if not allowed_cloud or inputs.get("cloud_id") != allowed_cloud:
        raise ValueError("cloud scope mismatch")
    if not scope.get("allowed_folder_id") or inputs.get("folder_id") != scope["allowed_folder_id"]:
        raise ValueError("folder scope mismatch")
    if inputs.get("environment") not in ("dev", "pilot"):
        raise ValueError("production is outside M1 scope")
    accounts = scope.get("allowed_environment_accounts", {}).get(inputs["environment"], {})
    for key in ("runtime_service_account_id", "timer_service_account_id"):
        if not accounts.get(key) or inputs.get(key) != accounts[key]:
            raise ValueError("service account scope mismatch")
    identity = inputs.get("identity")
    if identity is None and scope.get("allowed_environment_identity", {}).get(inputs["environment"]):
        raise ValueError("configured identity cannot be silently disabled")
    if identity is not None:
        allowed_identity = scope.get("allowed_environment_identity", {}).get(inputs["environment"], {})
        identity_keys = {
            "client_id", "issuer", "auth_service_account", "client_secret_id",
            "client_secret_version", "transaction_secret_id", "transaction_version",
        }
        if not isinstance(identity, dict) or set(identity) != identity_keys | {"code_sha256"}:
            raise ValueError("identity input scope mismatch")
        for key in identity_keys:
            if not isinstance(identity[key], str) or not identity[key] or identity[key] != allowed_identity.get(key):
                raise ValueError("identity resource scope mismatch")
        if identity["auth_service_account"] in accounts.values():
            raise ValueError("identity service account must be separate")
        if identity["client_secret_id"] == identity["transaction_secret_id"]:
            raise ValueError("identity secrets must be separate")
        digest = identity["code_sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("identity artifact digest required")
    if backend_path is not None:
        backend = json.loads(Path(backend_path).read_text())
        if not scope.get("allowed_state_bucket") or backend.get("bucket") != scope["allowed_state_bucket"]:
            raise ValueError("state bucket scope mismatch")
        if backend.get("endpoints", {}).get("s3") != "https://storage.yandexcloud.net":
            raise ValueError("unexpected state service endpoint")
        if backend.get("key") != f"{inputs['environment']}/terraform.tfstate":
            raise ValueError("state namespace mismatch")
        if backend.get("use_lockfile") is not True:
            raise ValueError("state locking required")


if __name__ == "__main__":
    try:
        if len(sys.argv) not in (3, 4):
            raise ValueError("private file paths required")
        check(*sys.argv[1:])
    except (ValueError, OSError, KeyError, TypeError, AttributeError):
        print("Deployment scope validation failed", file=sys.stderr)
        sys.exit(1)
    print("Deployment scope validated")
