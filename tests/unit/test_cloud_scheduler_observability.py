"""Timer delivery must not hide failed work or reset the invocation budget."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from zont_analyzer.cloud import scheduler
from zont_analyzer.observability import capture


def _scheduler(monkeypatch):
    db = Mock()
    db.jobs.acquire.return_value = SimpleNamespace(checkpoint=None, attempt=1)
    db.jobs.checkpoint.return_value = True
    db.storage.execute.return_value = [SimpleNamespace(rows=[])]
    db.get_app_meta.return_value = "1"
    service = scheduler.ProductionScheduler(
        SimpleNamespace(db=db), runner=Mock(),
        now=lambda: datetime(2026, 9, 27, tzinfo=UTC), monotonic=lambda: 0,
    )
    monkeypatch.setattr(service, "_sync", Mock(return_value=None))
    monkeypatch.setattr(service, "_reports", Mock(return_value=None))
    monkeypatch.setattr("zont_analyzer.cloud.user_jobs.scheduled_review", Mock(return_value={"status": "not_due"}))
    return service


@pytest.mark.parametrize(("result", "raised", "expected"), [
    ({"status": "done", "sync": {"failed_windows": 0}}, False, 1.0),
    ({"status": "pending", "sync": {"failed_windows": 1}}, False, 0.0),
    ({"status": "pending", "collection": {"failed_windows": 1}}, False, 0.0),
    ({"status": "error"}, False, 0.0),
    (None, True, 0.0),
])
def test_timer_records_lane_outcome_after_durable_progress(monkeypatch, result, raised, expected) -> None:
    service = _scheduler(monkeypatch)
    service._sync.return_value = result
    if raised:
        service._sync.side_effect = RuntimeError("private provider detail")
    measurements = []
    with capture(measurements.append):
        outcome = service.run()
    assert outcome["lane"] == "sync"
    assert ("zont_cloud_job_success", expected, {"operation": "scheduler_sync"}) in measurements
    assert ("zont_invocations_total", 1.0,
            {"operation": "scheduler_sync", "outcome": "success" if expected else "failure"}) in measurements
    assert any(name == "zont_cloud_job_observed_timestamp_seconds" and value > 0
               and labels == {"operation": "scheduler_sync"} for name, value, labels in measurements)
    assert len(measurements) == 3
    assert "private" not in str(outcome) + str(measurements)
    checkpoint = service.runtime.db.jobs.checkpoint.call_args.args[3]
    assert '"next_lane": 1' in checkpoint
    if raised:
        assert '"last_error"' in checkpoint


def test_idle_or_busy_timer_does_not_erase_previous_lane_failure(monkeypatch) -> None:
    service = _scheduler(monkeypatch)
    measurements = []
    with capture(measurements.append):
        assert service.run()["status"] == "idle"
        service._sync.return_value = {"status": "busy"}
        assert service.run()["status"] == "busy"
    assert measurements == []


@pytest.mark.parametrize(("startup_seconds", "remaining"), [(45.0, 525.0), (569.5, None)])
def test_runtime_initialization_consumes_the_same_outer_budget(monkeypatch, startup_seconds, remaining) -> None:
    runtime = Mock()
    monkeypatch.setattr(scheduler, "build_runtime", Mock(return_value=runtime))
    monkeypatch.setattr(scheduler.time, "monotonic", Mock(side_effect=[10.0, 10.0 + startup_seconds]))
    runner = Mock()
    runner.run.return_value = {"status": "idle"}
    constructor = Mock(return_value=runner)
    monkeypatch.setattr(scheduler, "ProductionScheduler", constructor)
    if remaining is None:
        with pytest.raises(TimeoutError, match="startup exhausted"):
            scheduler.execute({"_runtime_timeout_seconds": 570})
        constructor.assert_not_called()
    else:
        assert scheduler.execute({"_runtime_timeout_seconds": 570}) == {"status": "idle"}
        runner.run.assert_called_once_with(timeout_seconds=remaining)
    runtime.db.close.assert_called_once()


def test_scheduler_accepts_570_and_rejects_571(monkeypatch) -> None:
    service = _scheduler(monkeypatch)
    assert service.run(timeout_seconds=570)["status"] == "idle"
    with pytest.raises(ValueError, match="invalid scheduler timeout"):
        service.run(timeout_seconds=571)

    build_runtime = Mock()
    monkeypatch.setattr(scheduler, "build_runtime", build_runtime)
    with pytest.raises(ValueError, match="invalid scheduler timeout"):
        scheduler.execute({"_runtime_timeout_seconds": 571})
    build_runtime.assert_not_called()
