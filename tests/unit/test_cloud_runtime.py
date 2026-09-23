from __future__ import annotations

import base64
import http.client
import json
import multiprocessing
import os
import socket
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

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
        "status": "invalid",
        "error_type": "JobValidationError",
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
        "status": "ok",
    } in entries
    assert {
        "level": "ERROR",
        "message": "telemetry export failed",
        "event": "telemetry",
        "status": "failed",
        "error_type": "RuntimeError",
    } in entries


def test_unready_xray_blocks_jobs_but_not_diagnostics(server: Any) -> None:
    instance, credentials = server
    instance.tunnel.is_ready = False

    status, body = _request(instance, credentials, "POST", "/jobs/analytics", _payload())
    assert (status, body) == (503, {"error": "xray_unavailable"})
    status, body = _request(instance, credentials, "GET", "/diagnostics")
    assert status == 200
    assert body["xray_ready"] is False


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
