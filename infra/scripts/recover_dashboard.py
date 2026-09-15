"""Find only the dashboard created by the known provider metadata failure."""
import json
import sys
from pathlib import Path

import grpc
from bound_revision import identifier, request
from yandex.cloud.monitoring.v3.dashboard_service_pb2 import ListDashboardsRequest
from yandex.cloud.monitoring.v3.dashboard_service_pb2_grpc import DashboardServiceStub


def run(private):
    log = (private / "deploy-apply.log").read_text()
    if "CreateDashboardMetadata" not in log:
        raise ValueError("no supported dashboard creation failure in private apply log")
    scope = json.loads((private / "scope.json").read_text())
    inputs = json.loads((private / "cloud-work/inputs.tfvars.json").read_text())
    token = (private / "deploy-token").read_text().strip()
    folder_id = identifier(scope["allowed_folder_id"])
    folder = request(token, "resource-manager.api.cloud.yandex.net", f"/resource-manager/v1/folders/{folder_id}")
    if folder.get("cloudId") != scope["allowed_cloud_id"]:
        raise ValueError("folder outside authorized cloud")
    environment = inputs["environment"]
    if environment not in ("dev", "pilot"):
        raise ValueError("unsupported environment")
    name = f"zont-{environment}-probe"
    with grpc.secure_channel("monitoring.api.cloud.yandex.net:443", grpc.ssl_channel_credentials()) as channel:
        client = DashboardServiceStub(channel)
        result = client.List(ListDashboardsRequest(folder_id=folder_id, filter=f'name="{name}"', page_size=2),
                             timeout=20, metadata=[("authorization", "Bearer " + token),
                                                  ("clientid", "terraform-provider-yandex")])
    if len(result.dashboards) != 1 or result.next_page_token:
        raise ValueError("exactly one matching dashboard required; inspect privately")
    dashboard = result.dashboards[0]
    if dashboard.folder_id != folder_id or dashboard.name != name:
        raise ValueError("dashboard scope mismatch")
    (private / "dashboard-id").write_text(identifier(dashboard.id))
    print("Existing dashboard identified in the authorized folder")


if __name__ == "__main__":
    run(Path(sys.argv[1]))
