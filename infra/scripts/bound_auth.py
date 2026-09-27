"""Bound the optional auth function without provisioning idle instances."""
import json
import sys
from pathlib import Path

from bound_revision import identifier, request, wait_operation

HOST = "serverless-functions.api.cloud.yandex.net"
LIMITS = {"tag": "$latest", "zoneInstancesLimit": "1", "zoneRequestsLimit": "8",
          "provisionedInstancesCount": "0"}


def run(private):
    inputs = json.loads((private / "cloud-work/inputs.tfvars.json").read_text())
    if not inputs.get("identity"):
        return
    outputs = json.loads((private / "cloud-outputs.json").read_text())
    function_id = identifier(outputs["auth_function_id"]["value"])
    token = (private / "deploy-token").read_text().strip()
    function = request(token, HOST, f"/functions/v1/functions/{function_id}")
    if function.get("folderId") != inputs["folder_id"]:
        raise ValueError("auth function scope mismatch")
    operation = request(token, HOST, f"/functions/v1/functions/{function_id}:setScalingPolicy", LIMITS)
    wait_operation(token, private, operation, "auth_")
    policies = request(token, HOST, f"/functions/v1/functions/{function_id}/scalingPolicies")
    matches = [p for p in policies.get("scalingPolicies", []) if p.get("tag") == "$latest"]
    if len(matches) != 1 or any(str(matches[0].get(k, "0")) != v for k, v in LIMITS.items()):
        raise ValueError("auth scaling limits were not applied")
    (private / "auth-scaling.json").write_text(json.dumps(policies))
    print("Auth function limits verified; no provisioned instances")


if __name__ == "__main__":
    run(Path(sys.argv[1]))
