from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from zont_analyzer.cloud.runtime import JobTimeoutError, run_bounded
from zont_analyzer.cloud.telemetry import Telemetry
from zont_analyzer.observability import Measurement, capture, observe, span


def _instrumented_child(payload: dict[str, Any]) -> dict[str, Any]:
    observe("zont_openai_calls_total")
    observe("zont_sync_success", 0)
    if payload.get("timeout"):
        time.sleep(10)
    return {"status": "pending"}


def test_child_measurements_survive_success_and_timeout() -> None:
    events: list[Measurement] = []
    with capture(events.append):
        assert run_bounded(_instrumented_child, {}, 3) == {"status": "pending"}
        with pytest.raises(JobTimeoutError):
            run_bounded(_instrumented_child, {"timeout": True}, 2)
    assert [name for name, _, _ in events].count("zont_openai_calls_total") == 2
    assert ("zont_sync_success", 0.0, {}) in events


def test_measurements_are_isolated_bounded_and_nonfatal() -> None:
    events: list[Measurement] = []
    observe("zont_outside", 1)
    with capture(events.append):
        observe("zont_good", 2, operation="reports")
        observe("zont_bad", float("nan"))
        observe("zont_bad", 1, operation="https://private.example")
        with pytest.raises(ValueError), span("zont_analysis"):
            raise ValueError("private failure")
    assert events[0] == ("zont_good", 2.0, {"operation": "reports"})
    assert ("zont_analysis_success", 0.0, {}) in events
    assert len(events) == 4
    with capture(lambda _: (_ for _ in ()).throw(RuntimeError("sink unavailable"))):
        observe("zont_good")


def test_otlp_counters_accumulate_per_boot_and_gauges_keep_observation_time() -> None:
    telemetry = Telemetry(json.dumps({
        "endpoint": "https://otlp-gateway-test.grafana.net/otlp", "username": "123", "token": "test",
    }), "dev")
    telemetry.record(("zont_openai_calls_total", 1, {}))
    telemetry.record(("zont_openai_calls_total", 2, {}))
    telemetry.record(("zont_sync_success", 0, {}))
    first = telemetry.payload()["resourceMetrics"][0]
    second = telemetry.payload()["resourceMetrics"][0]
    metrics = {m["name"]: m for m in first["scopeMetrics"][0]["metrics"]}
    counter = metrics["zont_openai_calls_total"]["sum"]
    assert counter["aggregationTemporality"] == 2 and counter["isMonotonic"]
    assert [point["asDouble"] for point in counter["dataPoints"]] == [0, 3]
    assert int(counter["dataPoints"][0]["startTimeUnixNano"]) <= telemetry.started
    assert first["scopeMetrics"][0]["metrics"][1] == second["scopeMetrics"][0]["metrics"][1]
    assert first["resource"]["attributes"][1]["value"]["stringValue"] == telemetry.instance


def test_first_counter_after_long_idle_has_a_recent_distinct_zero() -> None:
    telemetry = Telemetry(json.dumps({
        "endpoint": "https://otlp-gateway-test.grafana.net/otlp", "username": "123", "token": "test",
    }), "dev")
    telemetry.started -= 2 * 3600 * 1_000_000_000
    before = time.time_ns()
    telemetry.record(("zont_ydb_errors_total", 1, {}))
    points = telemetry.payload()["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["sum"]["dataPoints"]
    assert before - 1_000_000 <= int(points[0]["timeUnixNano"])
    assert int(points[0]["timeUnixNano"]) // 1_000_000 < int(points[1]["timeUnixNano"]) // 1_000_000


def test_export_does_not_acknowledge_concurrently_recorded_series(monkeypatch: pytest.MonkeyPatch) -> None:
    telemetry = Telemetry(json.dumps({
        "endpoint": "https://otlp-gateway-test.grafana.net/otlp", "username": "123", "token": "test",
    }), "dev")
    process = SimpleNamespace(
        start=lambda: None, close=lambda: None, is_alive=lambda: False, exitcode=0,
        join=lambda _: telemetry.record(("zont_ydb_errors_total", 1, {})),
    )
    monkeypatch.setattr("zont_analyzer.cloud.telemetry.multiprocessing.get_context",
                        lambda _: SimpleNamespace(Process=lambda **_: process))
    telemetry.send(True, 1)
    metrics = telemetry.payload()["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    points = next(m for m in metrics if m["name"] == "zont_ydb_errors_total")["sum"]["dataPoints"]
    assert [p["asDouble"] for p in points] == [0, 1]
