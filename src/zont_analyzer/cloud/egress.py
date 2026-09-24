"""Explicit cloud routes and a supervised, loopback-only Xray client."""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import httpx

MAX_BYTES = 65536
MAX_REPORT_REQUEST_BYTES = 524_288
MAX_REPORT_RESPONSE_BYTES = 8_388_608


class ReportTransport(httpx.BaseTransport):
    """Route only approved report API calls, with bounded bodies and no ambient proxy."""

    def __init__(
        self, proxy_port: int = 1080, *, direct: httpx.BaseTransport | None = None,
        proxied: httpx.BaseTransport | None = None,
    ) -> None:
        if not 1024 <= proxy_port <= 65535:
            raise ValueError("invalid proxy port")
        self.direct = direct or httpx.HTTPTransport(trust_env=False)
        self.proxied = proxied or httpx.HTTPTransport(
            proxy=f"http://127.0.0.1:{proxy_port}", trust_env=False,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        target = request.url
        if (target.scheme != "https" or target.port not in (None, 443)
                or target.username or target.password or target.query or target.fragment):
            raise ValueError("destination denied")
        path = target.path
        if target.host == "my.zont.online" and request.method == "POST" and path in {
            "/api/devices", "/api/load_data", "/api/raw_events",
        }:
            transport = self.direct
        elif target.host == "api.openai.com" and (
            request.method == "POST" and path == "/v1/responses"
            or request.method == "GET" and path.startswith("/v1/models/")
        ):
            transport = self.proxied
        else:
            raise ValueError("destination denied")
        body = bytearray()
        for chunk in cast(httpx.SyncByteStream, request.stream):
            body.extend(chunk)
            if len(body) > MAX_REPORT_REQUEST_BYTES:
                raise ValueError("request byte limit")
        bounded = httpx.Request(request.method, target, headers=request.headers, content=bytes(body))
        response = transport.handle_request(bounded)
        try:
            if 300 <= response.status_code < 400:
                raise ValueError("redirect denied")
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > MAX_REPORT_RESPONSE_BYTES:
                    raise ValueError("response byte limit")
            return httpx.Response(response.status_code, headers=response.headers,
                                  content=bytes(content), request=request)
        finally:
            response.close()

    def close(self) -> None:
        self.direct.close()
        if self.proxied is not self.direct:
            self.proxied.close()


class UpstreamError(RuntimeError):
    """Public error contains no URL, credentials, headers or response data."""

    def __init__(self, status: int) -> None:
        super().__init__("upstream rejected request")
        self.status = status


class PolicyClient:
    def __init__(self, proxy_port: int = 1080) -> None:
        if not 1024 <= proxy_port <= 65535:
            raise ValueError("invalid proxy port")
        self.proxy_port = proxy_port

    def verify_openai_tls(self) -> None:
        """Verify CONNECT and certificate without sending an OpenAI HTTP request."""
        connection = http.client.HTTPSConnection("127.0.0.1", self.proxy_port, timeout=5)
        connection.set_tunnel("api.openai.com", 443)
        try:
            connection.connect()
        finally:
            connection.close()

    def request(
        self, url: str, *, method: str = "GET", headers: dict[str, str] | None = None,
        body: bytes = b"", timeout: float = 5,
    ) -> bytes:
        target = urlsplit(url)
        if (target.scheme != "https" or target.hostname not in {"api.openai.com", "my.zont.online"}
                or target.port not in (None, 443) or target.username or target.password
                or target.fragment or target.query):
            raise ValueError("destination denied")
        if target.hostname == "my.zont.online":
            if method != "POST" or target.path != "/api/devices":
                raise ValueError("ZONT smoke is read-only")
            proxy = None
        else:
            if method != "GET" or not target.path.startswith("/v1/models/"):
                raise ValueError("metadata request required")
            proxy = f"http://127.0.0.1:{self.proxy_port}"
        if len(body) > MAX_BYTES or not 0 < timeout <= 10:
            raise ValueError("invalid request budget")
        with (
            httpx.Client(proxy=proxy, trust_env=False, follow_redirects=False, timeout=timeout) as client,
            client.stream(method, url, headers=headers, content=body) as response,
        ):
            if response.status_code != 200:
                raise UpstreamError(response.status_code)
            result = bytearray()
            for chunk in response.iter_bytes():
                result.extend(chunk)
                if len(result) > MAX_BYTES:
                    raise ValueError("response byte limit")
            return bytes(result)


def validate_config(config: dict[str, Any], port: int) -> None:
    inbounds, outbounds = config.get("inbounds", []), config.get("outbounds", [])
    if (len(inbounds) != 1 or inbounds[0].get("listen") != "127.0.0.1"
            or inbounds[0].get("protocol") != "http" or inbounds[0].get("port") != port):
        raise ValueError("one loopback HTTP inbound required")
    if (not outbounds or outbounds[0].get("protocol") != "vless"
            or any(item.get("protocol") not in ("vless", "blackhole") for item in outbounds)):
        raise ValueError("only tunnel and reject outbounds permitted")
    if any(key in config for key in ("api", "metrics", "reverse", "observatory")):
        raise ValueError("unneeded Xray services forbidden")
    config["log"] = {"loglevel": "none", "access": "none", "dnsLog": False}


class Tunnel:
    def __init__(self, raw: str, port: int = 1080) -> None:
        config = json.loads(raw)
        validate_config(config, port)
        self.port = port
        self.process: subprocess.Popen[bytes] | None = None
        descriptor, self.path = tempfile.mkstemp(prefix="zont-xray-", suffix=".json")
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(config, stream)
            self.process = subprocess.Popen(
                ["xray", "run", "-c", self.path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 5
            while not self.ready():
                if self.process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("tunnel did not become ready")
                time.sleep(0.05)
        except Exception:
            self.close()
            raise

    def ready(self) -> bool:
        if self.process is None or self.process.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                return True
        except OSError:
            return False

    def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        Path(self.path).unlink(missing_ok=True)
