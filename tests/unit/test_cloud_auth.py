from __future__ import annotations

import base64
import http.client
import socket
import threading
import time
from typing import Any
from urllib.parse import urlencode

import pytest

import zont_analyzer.cloud.runtime as runtime_module
from zont_analyzer.cloud import auth
from zont_analyzer.cloud.runtime import CloudServer, RuntimeConfig


class _Tunnel:
    def ready(self) -> bool:
        return True


class _Database:
    def close(self) -> None:
        return


class _Runtime:
    def __init__(self) -> None:
        from zont_analyzer.config import AppConfig

        self.config = AppConfig()
        self.db = _Database()


@pytest.fixture
def server() -> Any:
    authorization = "Basic " + base64.b64encode(b"test-user:test-password").decode()
    config = RuntimeConfig("dev", 0, 3, "test", authorization)
    instance = CloudServer(("127.0.0.1", 0), config, _Tunnel(), runtime_factory=_Runtime)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.server_close()
    thread.join(timeout=2)


def _request(server: CloudServer, method: str, path: str, *, body: bytes | None = None,
             headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
    request_headers = {"Host": "app.example", **(headers or {})}
    try:
        connection.request(method, path, body, request_headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _login(server: CloudServer, username: str = "test-user", password: str = "test-password",
           *, origin: str = "https://app.example", path: str = "/login") -> tuple[int, dict[str, str], bytes]:
    return _request(server, "POST", path, body=urlencode({"username": username, "password": password}).encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": origin})


def test_session_is_signed_bounded_expiring_and_rotates_with_credentials() -> None:
    authorization = "Basic " + base64.b64encode(b"test-user:test-password").decode()
    token = auth.issue_session(authorization, now=1_000_000)
    assert auth.valid_session(token, authorization, now=1_000_001)
    assert auth.valid_session(token, authorization, now=1_000_000 + auth.SESSION_SECONDS - 1)
    assert not auth.valid_session(token, authorization, now=1_000_000 + auth.SESSION_SECONDS)
    assert not auth.valid_session(token, authorization, now=999_000)
    assert not auth.valid_session(token, authorization + "changed", now=1_000_001)
    assert not auth.valid_session(token[:-1] + ("A" if token[-1] != "A" else "B"), authorization,
                                  now=1_000_001)
    assert not auth.valid_session("A" * (auth.MAX_TOKEN_BYTES + 1), authorization)
    cookie = auth.session_cookie(token)
    assert cookie.startswith("__Host-zont_session=")
    assert all(flag in cookie for flag in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/", "Max-Age=43200"))
    assert "Domain=" not in cookie


def test_browser_login_cookie_and_unauthorized_pages(server: CloudServer, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def factory() -> _Runtime:
        calls.append(True)
        return _Runtime()

    server.runtime_factory = factory
    status, headers, html = _request(server, "GET", "/daily/2025-04-03.html")
    assert status == 401 and headers["Content-Type"].startswith("text/html")
    assert b"action='/login'" in html and b"test-user" not in html
    assert _request(server, "GET", "/za/latest.html")[0] == 401
    assert _request(server, "GET", "/reports.json")[1]["Content-Type"].startswith("application/json")
    assert _request(server, "GET", "/api/health")[0] == 401
    assert _request(server, "GET", "/ready")[0] == 401
    assert calls == []
    assert _request(server, "GET", "/login")[0] == 200

    status, headers, _body = _login(server, path="/login?next=https://evil.example/")
    assert status == 303 and headers["Location"] == "/"
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    assert "Secure" in headers["Set-Cookie"] and "HttpOnly" in headers["Set-Cookie"]
    assert _request(server, "GET", "/api/health", headers={"Cookie": cookie})[0] == 200
    assert _request(server, "GET", "/login", headers={"Cookie": cookie})[2].find(b"/logout") >= 0
    assert calls == [True]

    monkeypatch.setattr("zont_analyzer.cloud.site.serve", lambda _runtime, _path: (200, b"ok", "text/html"))
    assert _request(server, "GET", "/", headers={"Cookie": cookie})[0] == 200
    altered = cookie[:-1] + ("A" if cookie[-1] != "A" else "B")
    assert _request(server, "GET", "/", headers={"Cookie": altered})[0] == 401
    assert _request(server, "GET", "/api/health", headers={"Cookie": cookie + "; " + cookie})[0] == 401


def test_login_rejects_bad_credentials_and_cross_origin_without_cookie(server: CloudServer) -> None:
    for username, password, origin in (("test-user", "wrong", "https://app.example"),
                                       ("test-user", "test-password", "https://evil.example")):
        status, headers, body = _login(server, username, password, origin=origin)
        assert status == 401 and "Set-Cookie" not in headers and b"test-password" not in body
    status, headers, _body = _request(server, "POST", "/login", body=b"username=a&password=b",
                                       headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 401 and "Set-Cookie" not in headers
    status, headers, _body = _request(server, "POST", "/login", body=b"username=a&username=b&password=c",
                                       headers={"Content-Type": "application/x-www-form-urlencoded",
                                                "Origin": "https://app.example"})
    assert status == 401 and "Set-Cookie" not in headers


def test_logout_requires_origin_and_expires_cookie(server: CloudServer) -> None:
    status, headers, _body = _login(server)
    assert status == 303
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    assert _request(server, "POST", "/logout", headers={"Cookie": cookie})[0] == 403
    status, headers, _body = _request(server, "POST", "/logout", headers={
        "Cookie": cookie, "Origin": "https://app.example",
    })
    assert status == 303 and headers["Location"] == "/login"
    assert "Max-Age=0" in headers["Set-Cookie"] and "__Host-zont_session=" in headers["Set-Cookie"]


def test_login_form_body_stays_under_input_deadline(server: CloudServer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_module, "MAX_INPUT_SECONDS", 0.1)
    authorization = base64.b64encode(b"test-user:test-password").decode()
    head = ("POST /login HTTP/1.1\r\nHost: app.example\r\n"
            "Origin: https://app.example\r\nAuthorization: Basic " + authorization + "\r\n"
            "Content-Type: application/x-www-form-urlencoded\r\nContent-Length: 100\r\n\r\n").encode()
    with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=2) as connection:
        connection.sendall(head + b"username=a")
        connection.shutdown(socket.SHUT_WR)
        assert b" 400 " in connection.recv(1024)
    with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=2) as connection:
        connection.sendall(head + b"username=a")
        time.sleep(0.2)
        assert connection.recv(1024) == b""


def test_cookie_authorizes_same_origin_api_mutation_only(
    server: CloudServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zont_analyzer.cloud import user_jobs

    queued: list[tuple[str, str | None]] = []

    def enqueue(_runtime: Any, report_id: str, question: str | None) -> dict[str, Any]:
        queued.append((report_id, question))
        return {"report_id": report_id, "status": "queued"}

    monkeypatch.setattr(user_jobs, "enqueue_regeneration", enqueue)
    status, headers, _body = _login(server)
    assert status == 303
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    route = "/api/reports/daily-1/regenerate"
    request_headers = {"Cookie": cookie, "Content-Type": "application/json"}

    status, _headers, _body = _request(server, "POST", route, body=b"{}", headers={
        **request_headers, "Origin": "https://other.example",
    })
    assert status == 403 and queued == []

    status, _headers, _body = _request(server, "POST", route, body=b"{}", headers={
        **request_headers, "Origin": "https://app.example",
    })
    assert status == 202 and queued == [("daily-1", None)]
