from __future__ import annotations

import json
import time
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
    assert counter["dataPoints"][0]["startTimeUnixNano"] == str(telemetry.started)
    assert first["scopeMetrics"][0]["metrics"][1] == second["scopeMetrics"][0]["metrics"][1]
    assert first["resource"]["attributes"][1]["value"]["stringValue"] == telemetry.instance
