"""Apply scaling limits absent from the pinned Terraform provider, preserving revision settings."""
import copy
import http.client
import json
import re
import ssl
import sys
import time
from pathlib import Path

HOST = "serverless-containers.api.cloud.yandex.net"
LIMITS = {"zoneInstancesLimit": "1", "zoneRequestsLimit": "1"}
COPY_FIELDS = {
    "description", "resources", "executionTimeout", "serviceAccountId", "concurrency",
    "connectivity", "provisionPolicy", "secrets", "logOptions", "storageMounts", "mounts",
    "runtime", "metadataOptions", "asyncInvocationConfig",
}
READ_ONLY = {"id", "containerId", "createdAt", "status", "image", "scalingPolicy"}


class CloudError(ValueError):
    def __init__(self, status, body):
        super().__init__(f"cloud request failed: HTTP {status}")
        self.body = body


def revision_request(revision, container_id, inputs, image_key="probe_image"):
    if revision.get("containerId") != container_id or revision.get("status") != "ACTIVE":
        raise ValueError("revision does not match active deployment")
    if set(revision) - COPY_FIELDS - READ_ONLY:
        raise ValueError("unrecognized revision fields; refusing a lossy copy")
    if revision.get("serviceAccountId") != inputs["runtime_service_account_id"]:
        raise ValueError("runtime identity mismatch")
    image = copy.deepcopy(revision["image"])
    if image.get("imageUrl") != inputs[image_key]:
        raise ValueError("runtime image mismatch")
    if image.pop("imageDigest", None) != inputs[image_key].split("@", 1)[1]:
        raise ValueError("runtime digest mismatch")
    request = {key: copy.deepcopy(value) for key, value in revision.items() if key in COPY_FIELDS}
    # The API returns both compatibility representations for Object Storage mounts.
    # Sending the same mount twice is rejected; only remove exact duplicates.
    mounts = request.get("mounts", [])
    legacy = []
    for old in request.get("storageMounts", []):
        matches = [mount for mount in mounts if mount["mountPointPath"] == old["mountPointPath"]]
        if not matches:
            legacy.append(old)
            continue
        expected = {"mountPointPath": old["mountPointPath"],
                    "mode": "READ_ONLY" if old.get("readOnly") else "READ_WRITE",
                    "objectStorage": {"bucketId": old["bucketId"], "prefix": old.get("prefix", "")}}
        if len(matches) != 1 or matches[0] != expected:
            raise ValueError("conflicting mount representations")
    request["storageMounts"] = legacy
    request.update(containerId=container_id, imageSpec=image, scalingPolicy=dict(LIMITS))
    return request


def bounded(revision):
    policy = revision.get("scalingPolicy", {})
    return all(str(policy.get(key)) == value for key, value in LIMITS.items())


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9-]+", value):
        raise ValueError("invalid resource identifier")
    return value


def request(token, host, path, payload=None):
    connection = http.client.HTTPSConnection(host, timeout=20, context=ssl.create_default_context())
    try:
        connection.request("GET" if payload is None else "POST", path,
                           body=None if payload is None else json.dumps(payload),
                           headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        response = connection.getresponse()
        body = response.read(1024 * 1024 + 1)
        if response.status != 200 or len(body) > 1024 * 1024:
            raise CloudError(response.status, body)
        return json.loads(body)
    finally:
        connection.close()


def wait_operation(token, private, operation, prefix=""):
    operation_id = identifier(operation["id"])
    deadline = time.monotonic() + 240
    while not operation.get("done"):
        if time.monotonic() >= deadline:
            raise TimeoutError("scaling operation still pending; inspect its saved handle")
        time.sleep(3)
        operation = request(token, "operation.api.cloud.yandex.net", f"/operations/{operation_id}")
        (private / (prefix + "scaling-operation.json")).write_text(json.dumps(operation))
    if operation.get("error"):
        raise ValueError("scaling deployment failed; inspect private operation evidence")
    return operation


def run(private, application=False):
    scope = json.loads((private / "scope.json").read_text())
    inputs = json.loads((private / "cloud-work/inputs.tfvars.json").read_text())
    outputs = json.loads((private / "cloud-outputs.json").read_text())
    token = (private / "deploy-token").read_text().strip()
    folder_id = identifier(scope["allowed_folder_id"])
    folder = request(token, "resource-manager.api.cloud.yandex.net", f"/resource-manager/v1/folders/{folder_id}")
    if folder.get("cloudId") != scope["allowed_cloud_id"]:
        raise ValueError("folder outside authorized cloud")
    prefix = "application_" if application else ""
    image_key = "application_image" if application else "probe_image"
    container_id = identifier(outputs[prefix + "container_id"]["value"])
    container = request(token, HOST, f"/containers/v1/containers/{container_id}")
    if container.get("folderId") != folder_id:
        raise ValueError("container outside authorized folder")
    saved_operation = private / (prefix + "scaling-operation.json")
    if saved_operation.exists():
        operation = json.loads(saved_operation.read_text())
        if not operation.get("done"):
            wait_operation(token, private, operation, prefix)
    revisions = request(token, HOST, f"/containers/v1/revisions?containerId={container_id}"
                        "&filter=status%3D%22ACTIVE%22&pageSize=2")
    active = revisions.get("revisions", [])
    if len(active) != 1 or revisions.get("nextPageToken"):
        raise ValueError("exactly one active revision required")
    revision = active[0]
    payload = revision_request(revision, container_id, inputs, image_key)
    if not bounded(revision):
        operation = request(token, HOST, "/containers/v1/revisions:deploy", payload)
        # Record before polling so an interrupted observation can resume the same operation.
        saved_operation.write_text(json.dumps(operation))
        operation = wait_operation(token, private, operation, prefix)
        revision_id = identifier(operation["response"]["id"])
        revision = request(token, HOST, f"/containers/v1/revisions/{revision_id}")
    revision_request(revision, container_id, inputs, image_key)
    if not bounded(revision):
        raise ValueError("scaling limits not applied")
    (private / (prefix + "bounded-revision.json")).write_text(json.dumps(revision))
    print("Active revision scaling verified: one instance and request per zone")


if __name__ == "__main__":
    private_path = Path(sys.argv[1])
    try:
        run(private_path, application=len(sys.argv) == 3 and sys.argv[2] == "application")
    except CloudError as error:
        prefix = "application_" if len(sys.argv) == 3 and sys.argv[2] == "application" else ""
        (private_path / (prefix + "scaling-error.json")).write_bytes(error.body)
        sys.exit(str(error))
