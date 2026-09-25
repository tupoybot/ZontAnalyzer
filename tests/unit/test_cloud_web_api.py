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
