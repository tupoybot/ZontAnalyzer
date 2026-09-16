"""Bounded post-deployment check; never imports or runs the application."""
import http.client
import json
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


def check(host, path, token=None):
    connection = http.client.HTTPSConnection(host, timeout=35, context=ssl.create_default_context())
    try:
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        connection.request("GET" if token is None else "POST", path, headers=headers)
        response = connection.getresponse()
        return response.status, response.read(1024)
    finally:
        connection.close()


def run(private):
    outputs = json.loads((private / "cloud-outputs.json").read_text())
    host = target(outputs["probe_url"]["value"])
    token = (private / "deploy-token").read_text().strip()
    status, body = check(host, "/smoke", token)
    anonymous, _ = check(host, "/ready")
    result = {"smoke_http": status, "smoke_body_ok": body == b"ok\n", "anonymous_http": anonymous}
    (private / "runtime-smoke.json").write_text(json.dumps(result))
    if status != 200 or body != b"ok\n" or anonymous != 403:
        raise ValueError("runtime smoke failed; inspect private evidence")
    print("Runtime smoke passed; anonymous direct access denied")


if __name__ == "__main__":
    run(Path(sys.argv[1]))
