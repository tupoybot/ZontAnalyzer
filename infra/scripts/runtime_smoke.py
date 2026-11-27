"""Bounded post-deployment check; never imports or runs the application."""
import http.client
import json
import re
import ssl
import sys
from pathlib import Path
from urllib.parse import urlsplit


def target(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname
            or not parsed.hostname.endswith(".containers.yandexcloud.net")
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("unexpected container endpoint")
    return parsed.hostname


def require_monium_receipt(value):
    if not value or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", value):
        raise ValueError("Monium did not acknowledge the smoke event")
    return value


def check(host, path, token=None):
    connection = http.client.HTTPSConnection(host, timeout=35, context=ssl.create_default_context())
    try:
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        connection.request("GET" if token is None else "POST", path, headers=headers)
        response = connection.getresponse()
        return response.status, response.read(1024), response.getheader("X-Zont-Monium-Accepted")
    finally:
        connection.close()


def run(private):
    outputs = json.loads((private / "cloud-outputs.json").read_text())
    host = target(outputs["probe_url"]["value"])
    token = (private / "deploy-token").read_text().strip()
    status, body, receipt = check(host, "/smoke", token)
    anonymous, _, _ = check(host, "/ready")
    result = {"smoke_http": status, "smoke_body_ok": body == b"ok\n", "anonymous_http": anonymous}
    try:
        if status != 200 or body != b"ok\n" or anonymous != 403:
            raise ValueError("runtime smoke failed; inspect private evidence")
        result["monium_smoke_id"] = require_monium_receipt(receipt)
    finally:
        (private / "runtime-smoke.json").write_text(json.dumps(result))
    print("Runtime smoke passed; Monium accepted the probe event")


if __name__ == "__main__":
    run(Path(sys.argv[1]))
