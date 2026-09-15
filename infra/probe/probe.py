"""Isolated M1 infrastructure probe; never imports or runs the application."""
from __future__ import annotations

import base64
import contextlib
import hmac
import http.client
import json
import os
import signal
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from telemetry import Telemetry

MAX_BYTES = 65536
CONNECTIONS = threading.BoundedSemaphore(4)
METADATA_HOST = "169.254.169.254"
MONITORING_HOST = "monitoring.api.cloud.yandex.net"


class UpstreamStatusError(RuntimeError):
    def __init__(self, status):
        super().__init__("upstream status rejected")
        self.status = status


class PolicyClient:
    """Fixed routes, verified TLS after CONNECT, no redirects or env proxies."""

    def __init__(self, proxy_hosts, direct_hosts=(), proxy_port=1080):
        self.proxy_hosts = frozenset(proxy_hosts)
        self.direct_hosts = frozenset(direct_hosts)
        if self.proxy_hosts & self.direct_hosts:
            raise ValueError("ambiguous route")
        self.proxy_port = proxy_port

    def request(self, url, method="GET", body=None, headers=None, timeout=10,
                max_bytes=MAX_BYTES):
        target = urlsplit(url)
        if (target.scheme != "https" or target.username is not None
                or target.password is not None or target.fragment
                or target.port not in (None, 443)):
            raise ValueError("invalid destination")
        host = target.hostname
        if host not in self.proxy_hosts | self.direct_hosts:
            raise ValueError("destination denied")
        if method not in ("GET", "POST") or len(body or b"") > max_bytes:
            raise ValueError("request denied")
        if not 0 < timeout <= 20 or not 0 < max_bytes <= MAX_BYTES:
            raise ValueError("invalid request budget")
        deadline = time.monotonic() + timeout
        if not CONNECTIONS.acquire(timeout=min(5, timeout)):
            raise TimeoutError("connection limit")
        conn = None
        active_socket = None
        expired = threading.Event()

        def expire():
            expired.set()
            if active_socket is not None:
                with contextlib.suppress(OSError):
                    active_socket.shutdown(socket.SHUT_RDWR)

        timer = threading.Timer(max(0, deadline - time.monotonic()), expire)
        timer.daemon = True
        timer.start()
        try:
            via_proxy = host in self.proxy_hosts
            conn = http.client.HTTPSConnection(
                "127.0.0.1" if via_proxy else host,
                self.proxy_port if via_proxy else 443,
                timeout=min(5, max(0.01, deadline - time.monotonic())),
            )
            if via_proxy:
                conn.set_tunnel(host, 443)
            conn.connect()
            active_socket = conn.sock
            if expired.is_set():
                raise TimeoutError("request deadline")
            active_socket.settimeout(max(0.01, deadline - time.monotonic()))
            path = target.path or "/"
            if target.query:
                path += "?" + target.query
            request_headers = dict(headers or {})
            request_headers["Connection"] = "close"
            conn.request(method, path, body=body, headers=request_headers)
            response = conn.getresponse()
            if not 200 <= response.status < 300:
                raise UpstreamStatusError(response.status)
            chunks: list[bytes] = []
            remaining = max_bytes - len(body or b"")
            while True:
                if expired.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError("request deadline")
                if response.isclosed():
                    return b"".join(chunks)
                active_socket.settimeout(max(0.01, deadline - time.monotonic()))
                chunk = response.read1(min(8192, remaining + 1))
                if expired.is_set():
                    raise TimeoutError("request deadline")
                if not chunk:
                    return b"".join(chunks)
                remaining -= len(chunk)
                if remaining < 0:
                    raise ValueError("payload byte limit")
                chunks.append(chunk)
        finally:
            timer.cancel()
            if conn is not None:
                conn.close()
            CONNECTIONS.release()


def validate_config(config, proxy_port):
    inbounds = config.get("inbounds", [])
    outbounds = config.get("outbounds", [])
    if (len(inbounds) != 1 or inbounds[0].get("listen") != "127.0.0.1"
            or inbounds[0].get("protocol") != "http"
            or inbounds[0].get("port") != proxy_port):
        raise ValueError("one explicit loopback HTTP inbound required")
    if (not outbounds or outbounds[0].get("protocol") != "vless"
            or any(item.get("protocol") not in ("vless", "blackhole")
                   for item in outbounds)):
        raise ValueError("only tunnel and reject outbounds permitted")
    if any(key in config for key in ("api", "metrics", "reverse", "observatory")):
        raise ValueError("unneeded Xray services forbidden")
    config["log"] = {"loglevel": "none", "access": "none", "dnsLog": False}


class Tunnel:
    def __init__(self, raw, port):
        config = json.loads(raw)
        validate_config(config, port)
        self.port = port
        self.process = None
        descriptor, self.path = tempfile.mkstemp(prefix="m1-xray-", suffix=".json")
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(config, stream)
            self.process = subprocess.Popen(
                ["xray", "run", "-c", self.path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 5
            while not self.ready():
                if self.process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("tunnel did not become ready")
                time.sleep(0.05)
        except Exception:
            self.close()
            raise

    def ready(self):
        if self.process is None or self.process.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                return True
        except OSError:
            return False

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        Path(self.path).unlink(missing_ok=True)


def identity_token():
    # Only this fixed metadata path is permitted over HTTP.
    conn = http.client.HTTPConnection(METADATA_HOST, timeout=2)
    try:
        conn.request("GET", "/computeMetadata/v1/instance/service-accounts/default/token",
                     headers={"Metadata-Flavor": "Google"})
        response = conn.getresponse()
        if response.status != 200:
            raise RuntimeError("metadata access failed")
        data = response.read(16385)
        if len(data) > 16384:
            raise ValueError("metadata size exceeded")
        return json.loads(data)["access_token"]
    finally:
        conn.close()


def smoke(server):
    def phase(name, operation):
        server.last_phase = name
        try:
            result = operation()
        except Exception as error:
            print(json.dumps({"level": "INFO", "message": "m1-smoke", "phase": name,
                              "status": "failed", "error_type": type(error).__name__}), flush=True)
            raise
        print(json.dumps({"level": "INFO", "message": "m1-smoke", "phase": name, "status": "ok"}), flush=True)
        return result

    server.last_phase = "tunnel"
    if not server.tunnel.ready():
        raise RuntimeError("tunnel unavailable")
    phase("egress", lambda: server.client.request(server.smoke_url))
    token = phase("identity", identity_token)
    if phase("storage", lambda: Path("/publication/probe.txt").read_bytes()) != b"m1-private-object\n":
        raise RuntimeError("private object mismatch")
    query = urlencode({"folderId": os.environ["ZONT_FOLDER_ID"], "service": "custom"})
    result = phase("metric", lambda: server.client.request(
        f"https://{MONITORING_HOST}/monitoring/v2/data/write?{query}", "POST",
        json.dumps({"metrics": [{"name": "m1_probe_health", "value": 1}]}).encode(),
        {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    ))
    if int(json.loads(result).get("writtenMetricsCount", 0)) != 1:
        server.last_phase = "metric-confirmation"
        raise RuntimeError("metric write not confirmed")


class ProbeServer(ThreadingHTTPServer):
    telemetry: Telemetry | None = None
    tunnel: Tunnel
    client: PolicyClient
    smoke_url: str
    web_authorization: str
    last_phase: str = "not-started"


class Handler(BaseHTTPRequestHandler):
    server: ProbeServer
    def handle_request(self):
        self.connection.settimeout(5)
        if self.headers.get("Transfer-Encoding"):
            self.reply(400)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 <= size <= MAX_BYTES:
                raise ValueError()
        except ValueError:
            self.reply(413)
            return
        if len(self.rfile.read(size)) != size:
            self.reply(400)
            return
        if self.path == "/ready":
            self.reply(200 if self.server.tunnel.ready() else 503)
        elif self.path == "/smoke" and self.command == "POST":
            started = time.monotonic()
            try:
                smoke(self.server)
                if self.server.telemetry is not None:
                    self.server.last_phase = "grafana"
                    self.server.telemetry.send(self.server.client, True, time.monotonic() - started)
                self.reply(200)
            except Exception as error:  # noqa: BLE001 - expose only phase and error class
                if self.server.telemetry is not None and self.server.last_phase != "grafana":
                    try:
                        self.server.telemetry.send(self.server.client, False, time.monotonic() - started)
                    except Exception:  # noqa: BLE001 - retain original failure, never log secrets
                        print("Grafana failure metric export failed", flush=True)
                errno = getattr(error, "errno", None)
                status = getattr(error, "status", None)
                self.reply(502, f"failed:{self.server.last_phase}:{type(error).__name__}:{errno}:{status}\n".encode())
        elif self.path in ("/api/probe", "/private/probe.txt"):
            # Tests whether Basic Authorization survives the selected gateway.
            expected = self.server.web_authorization
            received = self.headers.get("Authorization", "")
            if not expected or not hmac.compare_digest(received, expected):
                self.reply(401)
                return
            if self.path == "/private/probe.txt":
                try:
                    if Path("/publication/probe.txt").read_bytes() != b"m1-private-object\n":
                        raise ValueError()
                except (OSError, ValueError):
                    self.reply(502)
                    return
            self.reply(200)
        else:
            self.reply(404)

    def reply(self, status, body=None):
        if body is None:
            body = b"ok\n" if status == 200 else b"failed\n"
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "text/plain")
        self.send_header("Connection", "close")
        if status == 401:
            self.send_header("WWW-Authenticate", 'Basic realm="M1 probe"')
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    do_GET = handle_request
    do_POST = handle_request

    def log_message(self, *_):
        pass


def main():
    if os.environ.get("ZONT_PROBE_ONLY") != "true":
        raise RuntimeError("probe mode required")
    smoke_url = os.environ["PROBE_SMOKE_URL"]
    smoke_host = urlsplit(smoke_url).hostname
    if smoke_host in ("api.openai.com", "developers.openai.com"):
        raise ValueError("M1 requires a synthetic endpoint")
    port = int(os.environ.get("PROBE_PROXY_PORT", "1080"))
    tunnel = Tunnel(os.environ.pop("XRAY_CONFIG"), port)
    try:
        server = ProbeServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler)
        server.tunnel = tunnel
        server.smoke_url = smoke_url
        telemetry_config = os.environ.pop("GRAFANA_OTLP_CONFIG", "")
        server.telemetry = Telemetry(telemetry_config, os.environ["ZONT_ENVIRONMENT"]) if telemetry_config else None
        direct_hosts = {MONITORING_HOST}
        if server.telemetry is not None:
            direct_hosts.add(server.telemetry.host)
        server.client = PolicyClient({smoke_host}, direct_hosts, port)
        credentials = os.environ.pop("PROBE_WEB_CREDENTIALS")
        server.web_authorization = "Basic " + base64.b64encode(credentials.encode()).decode()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
        try:
            server.serve_forever()
        finally:
            server.server_close()
    finally:
        tunnel.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - startup diagnostics must not expose secrets
        # Configuration errors can contain private hostnames or credentials.
        print("M1 probe startup failed", flush=True)
        raise SystemExit(1) from None
