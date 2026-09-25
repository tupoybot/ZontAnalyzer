from __future__ import annotations

import base64
import http.client
import json
import threading
from typing import Any

import pytest

import zont_analyzer.cloud.runtime as cloud_runtime
from zont_analyzer.cloud.runtime import CloudServer, RuntimeConfig
from zont_analyzer.cloud.web_api import _same_origin
from zont_analyzer.config import AppConfig


class _Tunnel:
    def ready(self) -> bool:
        return True


class _Database:
    def get_app_meta(self, _key: str) -> None:
        return None

    def close(self) -> None:
        return


class _Runtime:
    def __init__(self) -> None:
        self.config = AppConfig()
        self.db = _Database()


def _request(server: CloudServer, method: str, path: str, *,
             authorization: bool = True, body: dict[str, Any] | None = None,
             extra_headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    encoded = json.dumps(body).encode() if body is not None else None
    headers = {"Host": "app.example"}
    if authorization:
        headers["Authorization"] = "Basic " + base64.b64encode(b"owner:password").decode()
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        connection.request(method, path, encoded, headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.fixture
def server() -> Any:
    config = RuntimeConfig("dev", 0, 3, "test", "Basic " + base64.b64encode(b"owner:password").decode())
    instance = CloudServer(("127.0.0.1", 0), config, _Tunnel(), runtime_factory=_Runtime)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.server_close()
    thread.join(timeout=2)


def test_cloud_health_and_worker_health_use_existing_routes(server: CloudServer) -> None:
    assert _request(server, "GET", "/api/health") == (200, {"ok": True})
    assert _request(server, "GET", "/za/api/health") == (200, {"ok": True})
    assert _request(server, "GET", "/api/worker-health") == (503, {"ok": False})
    assert _request(server, "GET", "/api/health", authorization=False) == (
        401, {"error": "unauthorized"},
    )


def test_origin_check_requires_matching_scheme_host_and_port() -> None:
    assert _same_origin({"Origin": "https://app.example"}, "https://app.example")
    assert not _same_origin({"Origin": "http://app.example"}, "https://app.example")
    assert not _same_origin({"Origin": "https://other.example"}, "https://app.example")
    assert not _same_origin({"Origin": "null"}, "https://app.example")
    assert not _same_origin({"Sec-Fetch-Site": "cross-site"}, "https://app.example")


def test_feedback_write_uses_shared_route_and_checks_public_origin(
    server: CloudServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str | None]] = []

    def save(self: _Database, recommendation_id: str, status: str,
             note: str | None, *, experiment: Any = None) -> dict[str, Any]:
        calls.append((recommendation_id, status, note))
        return {"id": recommendation_id, "report_id": "daily-1", "status": status,
                "owner_note": note, "updated_at": "2026-09-25T00:00:00+00:00",
                "experiment": experiment}

    monkeypatch.setattr(_Database, "set_recommendation_feedback", save, raising=False)
    monkeypatch.setenv("CLOUD_PUBLIC_ORIGIN", "https://app.example")
    route = "/za/api/recommendations/rec-1/feedback"
    body = {"status": "applied", "owner_note": "Готово"}
    assert _request(server, "PUT", route, body=body, extra_headers={
        "Origin": "https://other.example",
    })[0] == 403
    assert calls == []
    status, value = _request(server, "PUT", route, body=body, extra_headers={
        "Origin": "https://app.example",
    })
    assert status == 200
    assert value["recommendation_id"] == "rec-1"
    assert calls == [("rec-1", "applied", "Готово")]


def test_only_private_timer_route_bypasses_basic_auth(
    server: CloudServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    def bounded(_handler: Any, payload: dict[str, Any], _seconds: float, _cancelled: Any) -> dict[str, Any]:
        seen.append(payload)
        return {"processed": 0}

    monkeypatch.setattr(cloud_runtime, "run_bounded", bounded)
    assert _request(server, "POST", "/jobs/maintenance", authorization=False, body={}) == (
        401, {"error": "unauthorized"},
    )
    status, result = _request(server, "POST", "/internal/maintenance", authorization=False,
                              body={"messages": [{"details": "ignored"}]})
    assert status == 200
    assert result["result"] == {"processed": 0}
    assert seen == [{"_runtime_timeout_seconds": 180}]
    assert _request(server, "GET", "/internal/maintenance", authorization=False) == (
        401, {"error": "unauthorized"},
    )


def test_api_get_and_complete_put_outlive_input_deadline(
    server: CloudServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    monkeypatch.setattr(cloud_runtime, "MAX_INPUT_SECONDS", 0.1)

    def slow_runtime() -> _Runtime:
        time.sleep(0.2)
        return _Runtime()

    def save(self: _Database, recommendation_id: str, status: str,
             note: str | None, *, experiment: Any = None) -> dict[str, Any]:
        return {"id": recommendation_id, "report_id": "daily-1", "status": status,
                "owner_note": note, "updated_at": "2026-09-25T00:00:00+00:00",
                "experiment": experiment}

    server.runtime_factory = slow_runtime
    monkeypatch.setattr(_Database, "set_recommendation_feedback", save, raising=False)
    assert _request(server, "GET", "/api/health") == (200, {"ok": True})
    status, value = _request(server, "PUT", "/api/recommendations/rec-1/feedback",
                             body={"status": "applied", "owner_note": "saved"})
    assert status == 200 and value["owner_note"] == "saved"


def test_api_body_must_finish_before_runtime_opens(
    server: CloudServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket
    import time

    monkeypatch.setattr(cloud_runtime, "MAX_INPUT_SECONDS", 0.1)
    calls = []

    def runtime() -> _Runtime:
        calls.append(True)
        return _Runtime()

    server.runtime_factory = runtime
    authorization = base64.b64encode(b"owner:password").decode()
    head = ("PUT /api/recommendations/rec-1/feedback HTTP/1.1\r\n"
            "Host: app.example\r\nAuthorization: Basic " + authorization + "\r\n"
            "Content-Type: application/json\r\nContent-Length: 30\r\n\r\n").encode()

    with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=2) as connection:
        connection.sendall(head + b'{}')
        connection.shutdown(socket.SHUT_WR)
        assert b" 400 " in connection.recv(1024)
    assert calls == []

    with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=2) as connection:
        connection.sendall(head + b'{"status":')
        time.sleep(0.2)
        assert connection.recv(1024) == b""
    assert calls == []


def test_model_accept_uses_approved_docs_transport_without_direct_network(
    server: CloudServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from zont_analyzer.application import ai_maintenance, ai_settings, model_review
    from zont_analyzer.cloud import user_jobs, web_api
    from zont_analyzer.cloud.egress import ReportTransport

    requested: list[str] = []
    decisions: list[tuple[str, str, int]] = []

    def deny_direct(_transport: Any, _request: httpx.Request) -> httpx.Response:
        raise AssertionError("cloud review used direct network transport")

    def deny_zont(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("cloud review used non-docs transport")

    def official_docs(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.host == "developers.openai.com"
        requested.append(request.url.path)
        if request.url.path == "/api/docs/models.md":
            return httpx.Response(200, text="Model ID gpt-6")
        if request.url.path == "/api/docs/deprecations.md":
            return httpx.Response(200, text="| Shutdown date | Deprecated model | Replacement |")
        if request.url.path == "/api/docs/models/gpt-6.md":
            return httpx.Response(200, text="Model ID: `gpt-6`")
        raise AssertionError("unexpected documentation path")

    class Settings:
        def __init__(self, _db: Any, _config: Any) -> None:
            return

        def view(self) -> dict[str, Any]:
            return {"version": 1}

    class Store:
        def __init__(self, _db: Any, catalog: Any, *, assessments: Any) -> None:
            self.catalog = catalog

        def decide(self, proposal_id: str, action: str, expected_version: int,
                   _settings: Any) -> None:
            self.catalog.fetch(model_ids=("gpt-6",))
            decisions.append((proposal_id, action, expected_version))

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_direct)
    monkeypatch.setattr(web_api, "ReportTransport", lambda: ReportTransport(
        direct=httpx.MockTransport(deny_zont), proxied=httpx.MockTransport(official_docs)))
    monkeypatch.setattr(web_api, "local_assessments", lambda _runtime: {})
    monkeypatch.setattr(ai_settings, "AISettingsStore", Settings)
    monkeypatch.setattr(model_review, "ModelReviewStore", Store)
    monkeypatch.setattr(web_api, "ModelReviewStore", Store)
    monkeypatch.setattr(ai_maintenance, "review_state", lambda _runtime: {"running": False})
    monkeypatch.setattr(user_jobs, "review_status", lambda _runtime: {"status": "idle"})

    status, value = _request(server, "PUT", "/api/ai/review", body={
        "action": "accept", "proposal_id": "proposal-1", "expected_version": 1,
    }, extra_headers={"Origin": "https://app.example"})
    assert status == 200 and value["review"]["job_status"] == "idle"
    assert decisions == [("proposal-1", "accept", 1)]
    assert requested == ["/api/docs/models.md", "/api/docs/deprecations.md", "/api/docs/models/gpt-6.md"]
