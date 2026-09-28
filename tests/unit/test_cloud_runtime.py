from __future__ import annotations

import base64
import http.client
import json
import multiprocessing
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

import zont_analyzer.cloud.runtime as runtime_module
from zont_analyzer.cloud.runtime import (
    DISPATCHERS,
    MAX_BODY_BYTES,
    MAX_RESULT_BYTES,
    CloudServer,
    JobFailureError,
    JobTimeoutError,
    RuntimeConfig,
    _dispatch_analytics,
    run_bounded,
)


class FakeTunnel:
    def __init__(self, ready: bool = True) -> None:
        self.is_ready = ready

    def ready(self) -> bool:
        return self.is_ready


def _slow(_payload: dict[str, Any]) -> dict[str, Any]:
    time.sleep(5)
    return {"unexpected": True}


def _briefly_slow(_payload: dict[str, Any]) -> dict[str, Any]:
    time.sleep(0.6)
    return {"done": True}


def _large_result(_payload: dict[str, Any]) -> dict[str, Any]:
    return {"data": "x" * 70_000}


def _too_large_result(_payload: dict[str, Any]) -> dict[str, Any]:
    return {"data": "x" * MAX_RESULT_BYTES}


def _die(_payload: dict[str, Any]) -> dict[str, Any]:
    os._exit(3)


@pytest.fixture
def server() -> Any:
    credentials = "test-user:test-password"
    config = RuntimeConfig(
        environment="dev",
        port=0,
        job_timeout_seconds=3,
        revision="test-revision",
        authorization="Basic " + base64.b64encode(credentials.encode()).decode(),
    )
    instance = CloudServer(("127.0.0.1", 0), config, FakeTunnel())
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance, credentials
    instance.shutdown()
    instance.server_close()
    thread.join(timeout=2)


def _request(
    server: CloudServer, credentials: str, method: str, path: str, body: Any = None,
    *, content_type: str = "application/json",
) -> tuple[int, dict[str, Any]]:
    encoded = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": "Basic " + base64.b64encode(credentials.encode()).decode()}
    if encoded is not None:
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(encoded))
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        connection.request(method, path, encoded, headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def _payload() -> dict[str, object]:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    return {
        "period_start": start.isoformat(),
        "period_end": (start + timedelta(minutes=20)).isoformat(),
        "target_c": 22.0,
        "samples": [
            {"timestamp": start.isoformat(), "value": 21.0},
            {"timestamp": (start + timedelta(minutes=10)).isoformat(), "value": 22.0},
            {"timestamp": (start + timedelta(minutes=20)).isoformat(), "value": 22.0},
        ],
    }


def test_all_routes_require_basic_auth_and_ready_is_local(server: Any) -> None:
    instance, credentials = server
    status, body = _request(instance, "wrong", "GET", "/ready")
    assert (status, body) == (401, {"error": "unauthorized"})

    status, body = _request(instance, credentials, "GET", "/ready")
    assert (status, body) == (200, {"ready": True})
    status, body = _request(instance, credentials, "GET", "/diagnostics")
    assert body["revision"] == "test-revision"
    assert body["counters"] == {"successes": 0, "failures": 0, "timeouts": 0}
    assert body["writes_enabled"] is True


@pytest.mark.parametrize("value", [None, "true", "false"])
def test_write_gate_environment(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    monkeypatch.setenv("CLOUD_ENVIRONMENT", "dev")
    monkeypatch.setenv("CLOUD_WEB_CREDENTIALS", "test-user:test-password")
    if value is None:
        monkeypatch.delenv("CLOUD_WRITES_ENABLED", raising=False)
    else:
        monkeypatch.setenv("CLOUD_WRITES_ENABLED", value)
    assert RuntimeConfig.from_environment().writes_enabled is (value != "false")


@pytest.mark.parametrize("value", ["", "TRUE", "False", "1", "0", " false ", "invalid"])
def test_write_gate_rejects_invalid_environment(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("CLOUD_ENVIRONMENT", "dev")
    monkeypatch.setenv("CLOUD_WRITES_ENABLED", value)
    with pytest.raises(ValueError, match="CLOUD_WRITES_ENABLED must be true or false"):
        RuntimeConfig.from_environment()


@pytest.mark.parametrize(
    ("method", "path"),
    [(method, prefix + "/settings") for method in ("POST", "PUT") for prefix in ("/api", "/za/api")]
    + [("POST", "/jobs/" + job) for job in
       ("analytics", "integrations", "reports", "maintenance", "publication", "scheduler")]
    + [("POST", "/internal/maintenance"), ("POST", "/internal/scheduler")],
)
def test_maintenance_blocks_writes_before_database_or_dispatch(
    server: Any, monkeypatch: pytest.MonkeyPatch, method: str, path: str,
) -> None:
    instance, credentials = server
    instance.config = replace(instance.config, writes_enabled=False)
    instance.runtime_factory = Mock(side_effect=AssertionError("database must not open"))
    dispatch = Mock(side_effect=AssertionError("jobs must not run"))
    monkeypatch.setattr(runtime_module, "run_bounded", dispatch)
    # The private timer endpoint must be blocked even without Basic credentials.
    if path.startswith("/internal/"):
        credentials = "wrong"
    assert _request(instance, credentials, method, path, {}) == (503, {"error": "maintenance"})
    instance.runtime_factory.assert_not_called()
    dispatch.assert_not_called()


def test_maintenance_keeps_authentication_and_diagnostics(server: Any) -> None:
    instance, credentials = server
    instance.config = replace(instance.config, writes_enabled=False)
    assert _request(instance, "wrong", "PUT", "/api/settings", {}) == (401, {"error": "unauthorized"})
    status, body = _request(instance, credentials, "GET", "/diagnostics")
    assert status == 200
    assert body["writes_enabled"] is False


def test_maintenance_keeps_login_and_logout(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _credentials = server
    instance.config = replace(instance.config, writes_enabled=False)
    monkeypatch.delenv("CLOUD_PUBLIC_ORIGIN", raising=False)
    origin = f"https://127.0.0.1:{instance.server_address[1]}"
    connection = http.client.HTTPConnection("127.0.0.1", instance.server_address[1], timeout=5)
    try:
        connection.request("POST", "/login", "username=test-user&password=test-password", {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": origin,
        })
        response = connection.getresponse()
        response.read()
        assert response.status == 303
        assert response.getheader("Set-Cookie")
        connection.request("POST", "/logout", headers={"Content-Length": "0", "Origin": origin})
        response = connection.getresponse()
        response.read()
        assert response.status == 303
        assert "Max-Age=0" in (response.getheader("Set-Cookie") or "")
    finally:
        connection.close()


@pytest.mark.parametrize("path", ["/jobs/monitoring", "/internal/monitoring"])
def test_maintenance_allows_read_only_monitoring(
    server: Any, monkeypatch: pytest.MonkeyPatch, path: str,
) -> None:
    instance, credentials = server
    instance.config = replace(instance.config, writes_enabled=False)
    instance.tunnel.is_ready = False
    dispatch = Mock(return_value={"healthy": True})
    monkeypatch.setattr(runtime_module, "run_bounded", dispatch)
    status, body = _request(instance, credentials, "POST", path, {})
    assert status == 200
    assert body["result"] == {"healthy": True}
    assert dispatch.call_args.args[0] is DISPATCHERS["monitoring"]


@pytest.mark.parametrize("path", ["/api/health", "/za/api/health"])
def test_maintenance_allows_api_reads(server: Any, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    instance, credentials = server
    instance.config = replace(instance.config, writes_enabled=False)
    application = SimpleNamespace(db=Mock())
    instance.runtime_factory = Mock(return_value=application)

    def handle(handler: Any, runtime: Any) -> bool:
        assert runtime is application
        handler._reply(200, {"healthy": True})
        return True

    monkeypatch.setattr("zont_analyzer.cloud.web_api.handle", handle)
    assert _request(instance, credentials, "GET", path) == (200, {"healthy": True})
    instance.runtime_factory.assert_called_once()
    application.db.close.assert_called_once()


def test_analytics_job_is_repeatable_and_rejects_malformed_input(
    server: Any, caplog: pytest.LogCaptureFixture
) -> None:
    instance, credentials = server
    first_status, first = _request(instance, credentials, "POST", "/jobs/analytics", _payload())
    second_status, second = _request(instance, credentials, "POST", "/jobs/analytics", _payload())

    assert first_status == second_status == 200
    assert first["job_id"] != second["job_id"]
    assert first["result"]["quality"] == second["result"]["quality"]
    with caplog.at_level("INFO"):
        status, body = _request(instance, credentials, "POST", "/jobs/analytics", {"private": "secret-value"})
    assert status == 400
    assert body["error_type"] == "JobValidationError"
    entry = json.loads(caplog.records[-1].message)
    assert entry == {
        "level": "ERROR",
        "message": "cloud job",
        "event": "cloud_job",
        "job_id": body["job_id"],
        "operation": "analytics",
        "status": "invalid",
        "error_type": "JobValidationError",
        "original_error_type": "ValidationError",
    }
    assert "secret-value" not in caplog.text
    status, body = _request(instance, credentials, "POST", "/jobs/analytics", _payload(), content_type="text/plain")
    assert (status, body) == (415, {"error": "json_content_type_required"})


def test_telemetry_failure_does_not_replace_completed_result(server: Any, caplog: pytest.LogCaptureFixture) -> None:
    class FailingTelemetry:
        def send(self, _success: bool, _duration: float) -> None:
            raise RuntimeError("private telemetry token")

    instance, credentials = server
    instance.telemetry = FailingTelemetry()
    with caplog.at_level("INFO"):
        status, body = _request(instance, credentials, "POST", "/jobs/analytics", _payload())

    assert status == 200
    assert "result" in body
    assert "private telemetry token" not in caplog.text
    entries = [json.loads(record.message) for record in caplog.records]
    assert {
        "level": "INFO",
        "message": "cloud job",
        "event": "cloud_job",
        "job_id": body["job_id"],
        "operation": "analytics",
        "status": "ok",
    } in entries
    assert {
        "level": "ERROR",
        "message": "telemetry export failed",
        "event": "telemetry",
        "status": "failed",
        "error_type": "RuntimeError",
    } in entries


@pytest.mark.parametrize(
    ("message", "expected_original_type"),
    [
        ("ConnectionResetError", "ConnectionResetError"),
        ("private error detail with token=secret-value", None),
        ("x" * 65, None),
    ],
)
def test_bounded_failure_log_has_operation_and_only_safe_original_class(
    server: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    message: str, expected_original_type: str | None,
) -> None:
    instance, credentials = server

    def fail(*_args: Any) -> None:
        raise JobFailureError(message)

    monkeypatch.setattr(runtime_module, "run_bounded", fail)
    with caplog.at_level("INFO"):
        status, body = _request(instance, credentials, "POST", "/jobs/reports", {})

    assert status == 502
    assert body["error"] == "job_failed"
    assert body["error_type"] == "JobFailureError"
    entry = next(json.loads(record.message) for record in caplog.records
                 if '"event":"cloud_job"' in record.message)
    assert entry["operation"] == "reports"
    assert entry["status"] == "failed"
    assert entry["error_type"] == "JobFailureError"
    assert entry.get("original_error_type") == expected_original_type
    if expected_original_type is None:
        assert message not in caplog.text


def test_unready_xray_blocks_jobs_but_not_diagnostics(server: Any) -> None:
    instance, credentials = server
    instance.tunnel.is_ready = False

    status, body = _request(instance, credentials, "POST", "/jobs/analytics", _payload())
    assert (status, body) == (503, {"error": "xray_unavailable"})
    status, body = _request(instance, credentials, "GET", "/diagnostics")
    assert status == 200
    assert body["xray_ready"] is False


def test_reports_route_uses_separate_bounded_budget_and_authorization(
    server: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, credentials = server
    instance.config = replace(instance.config, report_timeout_seconds=180)
    calls: list[tuple[dict[str, Any], float]] = []

    def bounded(_dispatch: Any, payload: dict[str, Any], timeout: float, _cancelled: Any) -> dict[str, Any]:
        calls.append((payload, timeout))
        return {"status": "pending", "phase": "collect"}

    monkeypatch.setattr(runtime_module, "run_bounded", bounded)
    request = {"kind": "daily", "date": "2026-09-23", "use_ai": False}
    assert _request(instance, "wrong", "POST", "/jobs/reports", request)[0] == 401
    status, body = _request(instance, credentials, "POST", "/jobs/reports", request)
    assert status == 200 and body["result"]["status"] == "pending"
    assert calls == [({**request, "_runtime_timeout_seconds": 180}, 180)]
    _request(instance, credentials, "POST", "/jobs/analytics", _payload())
    assert calls[1][1] == 3


@pytest.mark.parametrize("path", ["/jobs/scheduler", "/internal/scheduler"])
def test_scheduler_uses_report_budget_and_private_timer_ignores_payload(
    server: Any, monkeypatch: pytest.MonkeyPatch, path: str,
) -> None:
    instance, credentials = server
    instance.config = replace(instance.config, report_timeout_seconds=120)
    dispatch = Mock(return_value={"status": "idle"})
    monkeypatch.setattr(runtime_module, "run_bounded", dispatch)
    if path == "/jobs/scheduler":
        assert _request(instance, "wrong", "POST", path, {})[0] == 401
        dispatch.assert_not_called()
        payload = {}
    else:
        credentials = "wrong"
        payload = {"untrusted_timer_data": "must be ignored", "_runtime_timeout_seconds": 999}
    status, body = _request(instance, credentials, "POST", path, payload)
    assert status == 200
    assert body["result"] == {"status": "idle"}
    dispatch.assert_called_once_with(
        DISPATCHERS["scheduler"], {"_runtime_timeout_seconds": 120}, 120, instance.stopping,
    )


def test_job_rejects_concurrency_and_recovers_after_timeout(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, credentials = server
    monkeypatch.setitem(DISPATCHERS, "analytics", _briefly_slow)
    first: list[tuple[int, dict[str, Any]]] = []
    def first_request() -> None:
        first.append(_request(instance, credentials, "POST", "/jobs/analytics", _payload()))

    thread = threading.Thread(target=first_request)
    thread.start()
    for _ in range(100):
        if not instance.active_job.acquire(blocking=False):
            break
        instance.active_job.release()
        time.sleep(0.01)
    else:
        pytest.fail("first job was not active")
    assert _request(instance, credentials, "POST", "/jobs/analytics", _payload()) == (409, {"error": "job_busy"})
    thread.join(timeout=3)
    assert first[0][0] == 200

    instance.config = replace(instance.config, job_timeout_seconds=0.5)
    monkeypatch.setitem(DISPATCHERS, "analytics", _slow)
    status, body = _request(instance, credentials, "POST", "/jobs/analytics", _payload())
    assert status == 504
    assert body["error"] == "job_timeout"
    instance.config = replace(instance.config, job_timeout_seconds=3)
    monkeypatch.setitem(DISPATCHERS, "analytics", _dispatch_analytics)
    assert _request(instance, credentials, "POST", "/jobs/analytics", _payload())[0] == 200


def test_body_limits_and_transfer_encoding_are_rejected(server: Any) -> None:
    instance, credentials = server
    status, body = _request(instance, credentials, "POST", "/jobs/analytics", {"x": "z" * MAX_BODY_BYTES})
    assert (status, body) == (413, {"error": "invalid_body_size"})

    authorization = base64.b64encode(credentials.encode()).decode()
    connection = socket.create_connection(("127.0.0.1", instance.server_address[1]), timeout=5)
    try:
        connection.sendall(
            b"POST /jobs/analytics HTTP/1.1\r\nHost: test\r\nAuthorization: Basic " + authorization.encode()
            + b"\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
        )
        assert b" 400 " in connection.recv(4096)
    finally:
        connection.close()


def test_slow_headers_are_cut_off_by_the_total_input_deadline(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _credentials = server
    monkeypatch.setattr(runtime_module, "MAX_INPUT_SECONDS", 0.1)
    connection = socket.create_connection(("127.0.0.1", instance.server_address[1]), timeout=2)
    try:
        connection.sendall(b"GET /ready HTTP/1.1\r\n")
        time.sleep(0.2)
        assert connection.recv(1024) == b""
    finally:
        connection.close()


def test_timeout_terminates_spawned_work() -> None:
    started = time.monotonic()
    with pytest.raises(JobTimeoutError):
        run_bounded(_slow, {}, 0.2)
    assert time.monotonic() - started < 2
    assert not any(
        process.is_alive() and process.name.startswith("SpawnProcess")
        for process in multiprocessing.active_children()
    )


def test_pipe_returns_large_output_and_fails_when_child_exits_without_a_result() -> None:
    assert len(run_bounded(_large_result, {}, 2)["data"]) == 70_000
    with pytest.raises(JobFailureError):
        run_bounded(_too_large_result, {}, 2)
    started = time.monotonic()
    with pytest.raises(JobFailureError):
        run_bounded(_die, {}, 2)
    assert time.monotonic() - started < 1


def test_repeated_jobs_close_each_parent_process_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    base_context = multiprocessing.get_context("spawn")
    processes: list[Any] = []

    class TrackingProcess:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.process = base_context.Process(*args, **kwargs)
            self.closed = False

        @property
        def exitcode(self) -> int | None:
            return self.process.exitcode

        def start(self) -> None:
            self.process.start()

        def is_alive(self) -> bool:
            return self.process.is_alive()

        def join(self, timeout: float | None = None) -> None:
            self.process.join(timeout)

        def terminate(self) -> None:
            self.process.terminate()

        def kill(self) -> None:
            self.process.kill()

        def close(self) -> None:
            self.closed = True
            self.process.close()

    class TrackingContext:
        def Pipe(self, *, duplex: bool) -> tuple[Any, Any]:  # noqa: N802 - multiprocessing API spelling
            return base_context.Pipe(duplex=duplex)

        def Process(self, *args: Any, **kwargs: Any) -> TrackingProcess:  # noqa: N802 - multiprocessing API spelling
            process = TrackingProcess(*args, **kwargs)
            processes.append(process)
            return process

    monkeypatch.setattr("zont_analyzer.cloud.runtime.multiprocessing.get_context", lambda _method: TrackingContext())
    for _ in range(3):
        assert run_bounded(_large_result, {}, 2)["data"]
    assert len(processes) == 3
    assert all(process.closed for process in processes)


def test_slow_published_get_outlives_input_deadline(
    server: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, credentials = server
    monkeypatch.setattr(runtime_module, "MAX_INPUT_SECONDS", 0.1)

    class Database:
        def close(self) -> None:
            return

    class Application:
        db = Database()

    def open_runtime() -> Application:
        time.sleep(0.2)
        return Application()

    def serve(_runtime: Any, _path: str) -> tuple[int, bytes, str]:
        time.sleep(0.2)
        return 200, b'{"reports":[]}', "application/json"

    instance.runtime_factory = open_runtime
    monkeypatch.setattr("zont_analyzer.cloud.site.serve", serve)
    status, body = _request(instance, credentials, "GET", "/reports.json")
    assert (status, body) == (200, {"reports": []})


@pytest.mark.parametrize(
    ("path", "error", "event", "message"),
    [
        ("/api/health", "api_unavailable", "cloud_api", "cloud API failed"),
        ("/reports.json", "site_unavailable", "cloud_site", "cloud site failed"),
    ],
)
def test_http_failure_exposes_only_error_type(
    server: Any, caplog: pytest.LogCaptureFixture,
    path: str, error: str, event: str, message: str,
) -> None:
    instance, credentials = server
    private_message = "fixture-secret-token at /synthetic/private-path"

    def broken_runtime() -> Any:
        raise RuntimeError(private_message)

    instance.runtime_factory = broken_runtime
    with caplog.at_level("ERROR", logger="zont_analyzer.cloud.runtime"):
        status, body = _request(instance, credentials, "GET", path)

    assert (status, body) == (502, {"error": error, "error_type": "RuntimeError"})
    entries = [json.loads(record.message) for record in caplog.records if record.name == runtime_module.__name__]
    assert entries == [{
        "level": "ERROR", "message": message, "event": event, "error_type": "RuntimeError",
    }]
    assert private_message not in caplog.text
    assert private_message not in json.dumps(body)


def _web_runtime_harness(monkeypatch: pytest.MonkeyPatch) -> Any:
    from zont_analyzer.adapters.ydb.database import YdbConfig
    from zont_analyzer.runtime import Runtime

    state = SimpleNamespace(
        target=YdbConfig("grpcs://unit.test:2135", "/unit", namespace="first"),
        initialize_calls=0, close_calls=0, fail_first=False, initialize_delay=0.0,
    )
    calls_lock = threading.Lock()

    class FakeDatabase:
        def __init__(self, target: YdbConfig) -> None:
            self.target = target

        def initialize(self) -> None:
            with calls_lock:
                state.initialize_calls += 1
                attempt = state.initialize_calls
            time.sleep(state.initialize_delay)
            if state.fail_first and attempt == 1:
                raise RuntimeError("transient schema failure")

        def close(self) -> None:
            with calls_lock:
                state.close_calls += 1

    loaded = SimpleNamespace(config=SimpleNamespace(storage=SimpleNamespace(namespace="application")))
    monkeypatch.setattr(runtime_module, "_web_schema_ready", set())
    monkeypatch.setattr("zont_analyzer.config.load_config", lambda *_args: loaded)
    monkeypatch.setattr("zont_analyzer.adapters.ydb.application.Database", FakeDatabase)
    monkeypatch.setattr(
        YdbConfig, "from_environment", classmethod(lambda _cls, *, namespace: state.target),
    )
    monkeypatch.setattr(
        Runtime, "maintain_recommendation_lifecycle",
        lambda *_args, **_kwargs: pytest.fail("web request must not run startup maintenance"),
    )
    return state


def test_web_schema_is_initialized_once_for_concurrent_first_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _web_runtime_harness(monkeypatch)
    state.initialize_delay = 0.05
    with ThreadPoolExecutor(max_workers=8) as workers:
        runtimes = list(workers.map(lambda _: runtime_module._cloud_application_runtime(), range(8)))

    assert state.initialize_calls == 1
    assert len({id(runtime.db) for runtime in runtimes}) == 8
    for runtime in runtimes:
        runtime.db.close()
    assert state.close_calls == 8


def test_web_schema_failure_closes_driver_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _web_runtime_harness(monkeypatch)
    state.fail_first = True
    with pytest.raises(RuntimeError, match="transient schema failure"):
        runtime_module._cloud_application_runtime()
    assert state.initialize_calls == state.close_calls == 1

    runtime = runtime_module._cloud_application_runtime()
    runtime.db.close()
    assert state.initialize_calls == state.close_calls == 2


def test_web_schema_cache_is_scoped_to_database_target(monkeypatch: pytest.MonkeyPatch) -> None:
    from zont_analyzer.adapters.ydb.database import YdbConfig

    state = _web_runtime_harness(monkeypatch)
    for _ in range(2):
        runtime_module._cloud_application_runtime().db.close()
    assert state.initialize_calls == 1

    state.target = YdbConfig("grpcs://unit.test:2135", "/unit", namespace="second")
    for _ in range(2):
        runtime_module._cloud_application_runtime().db.close()
    assert state.initialize_calls == 2
    assert state.close_calls == 4
