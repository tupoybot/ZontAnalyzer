"""Bounded M2 smoke; ordinary deployment never invokes external application APIs."""
import base64
import http.client
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

from bound_revision import identifier, request
from runtime_smoke import target

SAMPLE = {
    "period_start": "2026-01-01T00:00:00Z", "period_end": "2026-01-01T00:20:00Z", "target_c": 22,
    "samples": [{"timestamp": "2026-01-01T00:00:00Z", "value": 21},
                {"timestamp": "2026-01-01T00:10:00Z", "value": 22},
                {"timestamp": "2026-01-01T00:20:00Z", "value": 22}],
}


def invoke(host, path, authorization=None, body=None):
    conn = http.client.HTTPSConnection(host, timeout=30)
    try:
        headers = {"Content-Type": "application/json"}
        if authorization:
            headers["Authorization"] = authorization
        conn.request("GET" if body is None else "POST", path,
                     body=None if body is None else json.dumps(body), headers=headers)
        response = conn.getresponse()
        data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("smoke response too large")
        return response.status, data
    finally:
        conn.close()


def context(private):
    inputs = json.loads((private / "cloud-work/inputs.tfvars.json").read_text())
    outputs = json.loads((private / "cloud-outputs.json").read_text())
    host = inputs["test_domain"] if inputs["attach_domain"] else outputs["gateway_domain"]["value"]
    if urlsplit("https://" + host).hostname != host or "/" in host:
        raise ValueError("invalid gateway hostname")
    secret_id = identifier(outputs["probe_secret_id"]["value"])
    version = identifier(inputs["secret_version_id"])
    token = (private / "deploy-token").read_text().strip()
    payload = request(token, "payload.lockbox.api.cloud.yandex.net",
                      f"/lockbox/v1/secrets/{secret_id}/payload?versionId={version}")
    credentials = next(entry["textValue"] for entry in payload["entries"] if entry["key"] == "web_credentials")
    authorization = "Basic " + base64.b64encode(credentials.encode()).decode()
    return host, authorization, inputs, outputs


def run(private):
    host, authorization, inputs, outputs = context(private)
    assert invoke(host, "/ready")[0] == 401
    assert invoke(host, "/ready", authorization)[0] == 200
    before = invoke(host, "/diagnostics", authorization)
    assert before[0] == 200
    assert json.loads(before[1])["revision"] == inputs["application_revision"]
    first = invoke(host, "/jobs/analytics", authorization, SAMPLE)
    second = invoke(host, "/jobs/analytics", authorization, SAMPLE)
    assert first[0] == second[0] == 200
    one, two = json.loads(first[1]), json.loads(second[1])
    assert one["result"]["quality"] == two["result"]["quality"]
    assert one["job_id"] != two["job_id"]
    invalid = invoke(host, "/jobs/analytics", authorization, {"invalid": True})
    assert invalid[0] == 400
    direct = target(outputs["application_url"]["value"])
    assert invoke(direct, "/ready")[0] == 403
    result = {"revision": inputs["application_revision"], "image": inputs["application_image"],
              "ready_http": 200, "anonymous_gateway_http": 401, "anonymous_container_http": 403,
              "first_job": one["job_id"], "second_job": two["job_id"], "validation_http": 400,
              "external_api_requests": 0}
    (private / "application-smoke.json").write_text(json.dumps(result))
    print("M2 cloud smoke passed: auth, readiness, repeated analytics, validation and private ingress")


if __name__ == "__main__":
    run(Path(sys.argv[1]))
