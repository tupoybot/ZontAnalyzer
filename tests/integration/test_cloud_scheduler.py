"""Production timer contracts with real YDB and fixture-only external clients."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.integration.test_cloud_report_jobs import _runner
from zont_analyzer.adapters.openai.model_catalog import CatalogSnapshot
from zont_analyzer.application.collection import CollectionService
from zont_analyzer.application.ingestion import IngestionService
from zont_analyzer.application.period_schedule import schedule_signature, seasonal_daily_signature
from zont_analyzer.cloud import scheduler, user_jobs
from zont_analyzer.domain import SourceEvent, TelemetryPoint
from zont_analyzer.domain.periods import calendar_period


def _coverage(db, period) -> None:
    db.save_devices([{"id": "fixture"}])
    for source in ("temperature", "raw_events"):
        db.telemetry.write_window(device_id="fixture", data_type=source, start=period.start,
                                  end=period.observed_end, state="empty")


def _scheduler_state(db, state: dict) -> None:
    lease = db.jobs.acquire(scheduler._KEY, "test-setup", 30)
    assert lease is not None
    assert db.jobs.checkpoint(lease.job_key, lease.owner, lease.attempt, json.dumps(state))
    assert db.jobs.release(lease.job_key, lease.owner, lease.attempt)
    db.set_app_meta(scheduler._BASELINES, "1")


@pytest.mark.ydb
def test_overlap_replay_resumes_across_processes_with_budget_smaller_than_sources(tmp_path: Path) -> None:
    db, runner, client = _runner(tmp_path)
    end = datetime.now(UTC).replace(microsecond=0)
    start = end - timedelta(hours=2)
    db.save_devices([{"id": "fixture"}])
    for source in ("temperature", "raw_events"):
        db.telemetry.write_window(device_id="fixture", data_type=source, start=start, end=end, state="empty")
    checked_after = datetime.now(UTC)
    client.fail_once = True
    results = []
    for _ in range(12):
        # Construct a new service each time; only YDB retains overlap progress.
        result = IngestionService(db, client, runner.runtime.config).sync(
            now=end, max_requests=1, replay_checked_after=checked_after, start_at=start,
        )
        results.append(result)
        if result["complete"]:
            break
    assert results[-1]["complete"]
    assert len(results) == 9  # eight half-hour windows, plus the failed first request
    assert client.history_calls == 5 and client.event_calls == 4
    assert db.get_cursor("fixture", "temperature") == end
    assert db.get_cursor("fixture", "raw_events") == end
    again = IngestionService(db, client, runner.runtime.config).sync(
        now=end, max_requests=1, replay_checked_after=checked_after, start_at=start,
    )
    assert again["complete"] and again["requests"] == 0


@pytest.mark.ydb
def test_collection_deadline_does_not_begin_another_source_request(tmp_path: Path) -> None:
    db, runner, client = _runner(tmp_path)
    db.save_devices([{"id": "fixture"}])
    now = datetime.now(UTC)
    result = CollectionService(db, client, runner.runtime.config).ensure_period(
        now - timedelta(hours=2), now, now=now, deadline=15, monotonic=lambda: 0,
    )
    assert result["pending"] and not result["complete"] and result["requests"] == 0
    assert client.history_calls == client.event_calls == 0


@pytest.mark.ydb
def test_scheduled_repair_preserves_ai_in_its_only_atomic_save(tmp_path: Path, monkeypatch) -> None:
    db, runner, _client = _runner(tmp_path)
    period = calendar_period("daily", datetime(2026, 9, 23).date(), "UTC")
    _coverage(db, period)
    previous = runner.runtime.analysis(no_ai=True).analyze_period(period, use_ai=False)
    previous.ai_used = True
    previous.summary = "Existing interpretation"
    previous.context["ai_provenance"] = {"model": "fixture-original"}
    db.save_report(previous, previous.summary)
    event = SourceEvent(id="late", device_id="fixture", event_type="disconnected",
                        timestamp_utc=period.start + timedelta(hours=5))
    db.telemetry.write_window(device_id="fixture", data_type="raw_events", start=period.start,
                              end=period.end, events=[event], state="complete")
    assert scheduler.daily_needs_report(runner.runtime.analysis(no_ai=True), period.start.date(), period.start.date())
    assert runner.run_scheduled(period, use_ai=False)["phase"] == "analyze"
    saves = []
    original_save = db.save_report

    def save(report, text, **kwargs):
        assert report.ai_used and report.summary == previous.summary
        assert report.context["ai_provenance"] == previous.context["ai_provenance"]
        assert report.context["pilot_ai_reuse"]["facts_changed"] is True
        assert kwargs["job_fence"] and kwargs["write_fence"]
        saves.append(report)
        return original_save(report, text, **kwargs)

    monkeypatch.setattr(db, "save_report", save)
    assert runner.run_scheduled(period, use_ai=False)["status"] == "done"
    assert len(saves) == 1
    assert not scheduler.daily_needs_report(
        runner.runtime.analysis(no_ai=True), period.start.date(), period.start.date(),
    )
    assert runner.run_scheduled(period, use_ai=False)["reused"]
    assert len(saves) == 1


@pytest.mark.ydb
def test_seasonal_job_keeps_weekly_observed_boundary_and_resumes_saved_report(tmp_path: Path, monkeypatch) -> None:
    db, runner, _client = _runner(tmp_path)
    period = runner.runtime.analysis(no_ai=True).seasonal_period(
        2026, "autumn", as_of=datetime(2026, 9, 21, tzinfo=UTC),
    )
    _coverage(db, period)
    assert runner.run_scheduled(period, use_ai=False)["phase"] == "analyze"
    complete = db.jobs.complete
    monkeypatch.setattr(db.jobs, "complete", lambda *_args: False)
    with pytest.raises(RuntimeError, match="ownership expired"):
        runner.run_scheduled(period, use_ai=False)
    report = db.report(runner.runtime.analysis(no_ai=True).report_id_for("seasonal", period.start))
    assert report is not None
    assert report.period_end == period.observed_end < period.end
    assert report.context["period"]["complete"] is False
    assert report.context["schedule_signature"]
    monkeypatch.setattr(db.jobs, "complete", complete)
    original_analysis = runner.runtime.analysis

    def no_reanalysis(*args, **kwargs):
        assert kwargs.get("job_fence") is None
        return original_analysis(*args, **kwargs)

    monkeypatch.setattr(runner.runtime, "analysis", no_reanalysis)
    assert runner.run_scheduled(period, use_ai=False)["status"] == "done"


@pytest.mark.ydb
def test_scheduler_waits_for_daily_delay_and_uses_first_daily_ai_policy(tmp_path: Path) -> None:
    db, runner, _client = _runner(tmp_path)
    selected = datetime(2026, 9, 24, tzinfo=UTC)
    fake_runner = Mock()
    fake_runner.run_scheduled.return_value = {"status": "pending", "phase": "analyze"}
    service = scheduler.ProductionScheduler(runner.runtime, runner=fake_runner, monotonic=lambda: 0)
    state = {"last_sync": selected.isoformat()}
    assert service._reports("daily", state, selected + timedelta(minutes=59), 180, lambda: None) is None
    assert service._reports("daily", state, selected + timedelta(minutes=60), 180, lambda: None)["status"] == "pending"
    period = fake_runner.run_scheduled.call_args.args[0]
    assert period.start.date().isoformat() == "2026-09-23"
    assert fake_runner.run_scheduled.call_args.kwargs["use_ai"] is True
    # A restart keeps the original date even after the local date advances.
    next_service = scheduler.ProductionScheduler(runner.runtime, runner=fake_runner, monotonic=lambda: 0)
    next_service._reports("daily", state, selected + timedelta(days=1), 180, lambda: None)
    assert fake_runner.run_scheduled.call_args.args[0] == period


@pytest.mark.ydb
def test_scheduler_serializes_duplicate_timers_and_failed_lane_does_not_starve_review(
    tmp_path: Path, monkeypatch,
) -> None:
    db, runner, _client = _runner(tmp_path)
    service = scheduler.ProductionScheduler(runner.runtime, runner=runner, now=runner.now)
    _scheduler_state(db, {})
    lock = db.jobs.acquire(scheduler._KEY, "other-timer", 30)
    assert service.run()["status"] == "busy"
    assert db.jobs.release(lock.job_key, lock.owner, lock.attempt)

    def failed_sync(*_args):
        raise RuntimeError("private provider error must not appear in response")

    monkeypatch.setattr(service, "_sync", failed_sync)
    result = service.run()
    assert result["status"] == "error" and result["lane"] == "sync"
    assert "private" not in json.dumps(result)
    review = Mock(return_value={"status": "done"})
    monkeypatch.setattr("zont_analyzer.cloud.user_jobs.scheduled_review", review)
    monkeypatch.setattr(service, "_sync", Mock(return_value=None))
    assert service.run() == {"lane": "review", "status": "done"}
    review.assert_called_once()


@pytest.mark.ydb
def test_scheduler_resumes_same_sync_slot_then_respects_poll_interval(tmp_path: Path, monkeypatch) -> None:
    db, runner, _client = _runner(tmp_path)
    runner.runtime.config.scheduler.sync_every_minutes = 30
    service = scheduler.ProductionScheduler(runner.runtime, runner=runner)
    sync = Mock(side_effect=[{"complete": False}, {"complete": True}])
    monkeypatch.setattr(runner.runtime, "ingestion", lambda _client: Mock(sync=sync))
    state = {}
    start = datetime.now(UTC)
    assert service._sync(state, start, 9999999999, lambda: None)["status"] == "pending"
    assert service._sync(state, start + timedelta(minutes=1), 9999999999, lambda: None)["status"] == "done"
    assert sync.call_args_list[0].kwargs["now"] == sync.call_args_list[1].kwargs["now"]
    assert (sync.call_args_list[0].kwargs["replay_checked_after"]
            == sync.call_args_list[1].kwargs["replay_checked_after"])
    assert sync.call_args_list[0].kwargs["max_requests"] == 100
    assert service._sync(state, start + timedelta(minutes=29), 9999999999, lambda: None) is None


@pytest.mark.ydb
def test_completed_sync_can_share_timer_delivery_with_report_lane(tmp_path: Path, monkeypatch) -> None:
    db, runner, _client = _runner(tmp_path)
    _scheduler_state(db, {})
    service = scheduler.ProductionScheduler(runner.runtime, runner=runner)
    monkeypatch.setattr(service, "_sync", Mock(return_value={"status": "done", "sync": {"failed_windows": 0}}))
    reports = Mock(return_value={"status": "done"})
    monkeypatch.setattr(service, "_reports", reports)

    result = service.run()

    assert result["lane"] == "daily"
    reports.assert_called_once()


@pytest.mark.ydb
def test_baseline_initialization_is_bounded_and_precedes_source_reads(tmp_path: Path, monkeypatch) -> None:
    db, runner, client = _runner(tmp_path)
    analysis = runner.runtime.analysis(no_ai=True)
    for day in (20, 21, 22):
        analysis.analyze_daily(datetime(2026, 9, day).date(), use_ai=False)
    assert db.seed_source_event_report_baselines(batch_size=1) == 1
    assert db.get_app_meta(scheduler._BASELINES) is None
    assert db.seed_source_event_report_baselines(batch_size=1) == 1
    assert db.get_app_meta(scheduler._BASELINES) is None
    service = scheduler.ProductionScheduler(runner.runtime, runner=runner)
    assert service.run()["status"] == "initializing"
    assert db.get_app_meta(scheduler._BASELINES) == "1"
    assert client.history_calls == client.event_calls == 0


@pytest.mark.ydb
def test_shared_report_write_fence_blocks_scheduled_and_manual_jobs(tmp_path: Path) -> None:
    db, runner, client = _runner(tmp_path)
    period = calendar_period("daily", datetime(2026, 9, 23).date(), "UTC")
    report_id = runner.runtime.analysis(no_ai=True).report_id_for("daily", period.start)
    lock = db.jobs.acquire("report-write:" + report_id, "manual-regeneration", 30)
    assert lock is not None
    assert runner.run_scheduled(period, use_ai=False)["status"] == "busy"
    assert runner.run({"kind": "daily", "date": "2026-09-23", "use_ai": False})["status"] == "busy"
    assert client.history_calls == client.event_calls == 0
    report = runner.runtime.analysis(no_ai=True).analyze_daily(period.start.date(), use_ai=False, persist=False)
    assert db.jobs.release(lock.job_key, lock.owner, lock.attempt)
    with pytest.raises(ValueError, match="write ownership expired"):
        db.save_report(report, report.summary, write_fence=(lock.job_key, lock.owner, lock.attempt))
    assert db.report(report_id) is None


def test_scheduler_rejects_payload_policy_overrides() -> None:
    with pytest.raises(ValueError, match="payload must be empty"):
        scheduler.execute({"use_ai": False})


@pytest.mark.ydb
def test_short_invocation_advances_deterministic_report_instead_of_waiting_for_ai_budget(tmp_path: Path) -> None:
    db, runner, _client = _runner(tmp_path)
    period = calendar_period("daily", datetime(2026, 9, 23).date(), "UTC")
    _coverage(db, period)
    state = {"last_sync": runner.now().isoformat(),
             "daily_pending": {"period": period.model_dump(mode="json"), "use_ai": False}}
    service = scheduler.ProductionScheduler(runner.runtime, runner=runner)
    import time

    assert service._reports("daily", state, runner.now(), time.monotonic() + 120, lambda: None)["phase"] == "analyze"
    assert service._reports("daily", state, runner.now(), time.monotonic() + 120, lambda: None)["status"] == "done"
    assert "daily_pending" not in state


@pytest.mark.ydb
def test_fixed_sync_start_repairs_older_hole_even_when_cursors_already_advanced(tmp_path: Path) -> None:
    db, runner, client = _runner(tmp_path)
    end = datetime.now(UTC).replace(microsecond=0)
    start = end - timedelta(hours=3)
    db.save_devices([{"id": "fixture"}])
    for source in ("temperature", "raw_events"):
        db.telemetry.write_window(device_id="fixture", data_type=source,
                                  start=end - timedelta(hours=2), end=end, state="empty")
    checked_after = datetime.now(UTC)
    client.fail_once = True
    for _ in range(16):
        result = IngestionService(db, client, runner.runtime.config).sync(
            now=end, max_requests=1, replay_checked_after=checked_after, start_at=start,
        )
        if result["complete"]:
            break
    assert result["complete"]
    assert client.history_calls == 7 and client.event_calls == 6
    assert db.telemetry.missing_intervals("fixture", "temperature", start, end, now=end) == []


@pytest.mark.ydb
def test_season_final_boundary_replaces_running_period_and_preserves_existing_ai(tmp_path: Path) -> None:
    db, runner, _client = _runner(tmp_path)
    analysis = runner.runtime.analysis(no_ai=True)
    running = analysis.seasonal_period(2026, "autumn", as_of=datetime(2026, 10, 19, tzinfo=UTC))
    previous = analysis.analyze_period(running, use_ai=False)
    previous.ai_used = True
    previous.summary = "Original seasonal analysis"
    db.save_report(previous, previous.summary)
    runner.now = lambda: datetime(2026, 12, 2, tzinfo=UTC)
    complete = analysis.seasonal_period(2026, "autumn", as_of=runner.now())
    _coverage(db, complete)
    assert runner.run_scheduled(complete, use_ai=False)["phase"] == "analyze"
    assert runner.run_scheduled(complete, use_ai=False)["status"] == "done"
    final = db.report(previous.id)
    assert final is not None and final.period_end == complete.end
    assert final.context["period"]["complete"] is True
    assert final.ai_used and final.summary == previous.summary
    assert len(db.storage.execute("SELECT id FROM reports;")[0].rows) == 1


@pytest.mark.ydb
def test_scheduled_review_respects_disabled_setting_interval_and_scheduled_trigger(tmp_path: Path, monkeypatch) -> None:
    db, runner, _client = _runner(tmp_path)
    catalog = Mock()
    catalog.fetch.return_value = CatalogSnapshot(datetime.now(UTC), (), (), incomplete=True, error="fixture outage")
    monkeypatch.setattr(user_jobs, "OpenAIModelCatalog", lambda **_kwargs: catalog)
    runner.runtime.config.openai.review_enabled = False
    assert user_jobs.scheduled_review(runner.runtime)["status"] == "not_due"
    catalog.fetch.assert_not_called()
    runner.runtime.config.openai.review_enabled = True
    assert user_jobs.scheduled_review(runner.runtime)["status"] == "done"
    assert user_jobs.scheduled_review(runner.runtime)["status"] == "not_due"
    catalog.fetch.assert_called_once()
    settings = user_jobs.AISettingsStore(db, runner.runtime.config).snapshot()
    history = user_jobs.ModelReviewStore(db, catalog).state(settings)["runs"]
    assert len(history) == 1 and history[0]["trigger"] == "scheduled"


@pytest.mark.ydb
def test_latest_daily_is_not_delayed_by_archive_cursor_and_unknown_ai_does_not_block_new_dates(tmp_path: Path) -> None:
    _db, runner, _client = _runner(tmp_path)
    fake_runner = Mock()
    fake_runner.run_scheduled.return_value = {"status": "reconciliation_required", "request_key": "original-request"}
    service = scheduler.ProductionScheduler(runner.runtime, runner=fake_runner, monotonic=lambda: 0)
    state = {"last_sync": runner.now().isoformat(), "daily_cursor": 45}
    service._reports("daily", state, runner.now(), 180, lambda: None)
    first = fake_runner.run_scheduled.call_args.args[0]
    assert first.start.date().isoformat() == "2026-09-23"
    assert "daily_pending" not in state and len(state["daily_deferred"]) == 1
    # Before reconciliation retry becomes due, another timer cannot dispatch
    # the unknown request again. A newer local date may still be reported.
    fake_runner.run_scheduled.reset_mock()
    assert service._reports("daily", state, runner.now(), 180, lambda: None) is None
    fake_runner.run_scheduled.assert_not_called()
    state["daily_deferred"][0]["retry_at"] = (runner.now() + timedelta(days=2)).isoformat()
    fake_runner.run_scheduled.return_value = {"status": "done"}
    service._reports("daily", state, runner.now() + timedelta(days=1), 180, lambda: None)
    assert fake_runner.run_scheduled.call_args.args[0].start.date().isoformat() == "2026-09-24"


@pytest.mark.ydb
def test_missing_daily_reports_preempt_an_older_pending_repair(tmp_path: Path, monkeypatch) -> None:
    db, runner, _client = _runner(tmp_path)
    now = datetime(2026, 9, 30, 2, tzinfo=UTC)
    old = calendar_period("daily", datetime(2026, 9, 26).date(), "UTC")
    monkeypatch.setattr(db, "earliest_sample_time", lambda: old.start)
    analysis = runner.runtime.analysis(no_ai=True)
    calls = []

    def finish(period, *, use_ai, timeout_seconds):
        calls.append((period.start.date(), use_ai))
        db.save_report(analysis.analyze_period(period, use_ai=False), "fixture")
        return {"status": "done"}

    fake_runner = Mock(run_scheduled=Mock(side_effect=finish))
    service = scheduler.ProductionScheduler(runner.runtime, runner=fake_runner, monotonic=lambda: 0)
    state = {"last_sync": now.isoformat(),
             "daily_pending": {"period": old.model_dump(mode="json"), "use_ai": False}}
    for _ in range(4):
        assert service._reports("daily", state, now, 180, lambda: None)["status"] == "done"
    assert calls == [
        (datetime(2026, 9, 29).date(), True),
        (datetime(2026, 9, 28).date(), False),
        (datetime(2026, 9, 27).date(), False),
        (old.start.date(), False),
    ]
    assert state["daily_deferred"] == []


@pytest.mark.ydb
def test_missing_daily_report_runs_before_other_report_lanes(tmp_path: Path, monkeypatch) -> None:
    db, runner, _client = _runner(tmp_path)
    now = datetime(2026, 9, 30, 2, tzinfo=UTC)
    monkeypatch.setattr(db, "earliest_sample_time", lambda: now - timedelta(days=3))
    _scheduler_state(db, {"last_sync": now.isoformat(), "next_lane": 2})
    fake_runner = Mock()
    fake_runner.run_scheduled.return_value = {"status": "pending", "phase": "collect"}
    service = scheduler.ProductionScheduler(runner.runtime, runner=fake_runner, now=lambda: now)
    monkeypatch.setattr(service, "_sync", lambda *_args: None)

    result = service.run()

    assert result["lane"] == "daily" and result["status"] == "pending"
    assert fake_runner.run_scheduled.call_args.args[0].start.date().isoformat() == "2026-09-29"
    assert json.loads(db.jobs.get(scheduler._KEY).checkpoint)["next_lane"] == 1


@pytest.mark.ydb
def test_long_season_waits_for_daily_inputs_and_detects_repaired_facts_without_ai_text(tmp_path: Path) -> None:
    db, runner, _client = _runner(tmp_path)
    runtime = runner.runtime
    runtime.config.pilot.max_catchup_days = 2
    now = datetime(2026, 10, 20, 2, tzinfo=UTC)
    first = now.replace(hour=0) - timedelta(days=2)
    db.save_devices([{"id": "fixture"}])
    point = TelemetryPoint(device_id="fixture", source_type="temperature", entity_id="room",
                           metric_key="temperature", timestamp_utc=first, value_num=20)
    db.telemetry.write_window(device_id="fixture", data_type="temperature", start=first,
                              end=first + timedelta(minutes=30), points=[point], state="complete")
    fake_runner = Mock()
    fake_runner.run_scheduled.return_value = {"status": "pending"}
    service = scheduler.ProductionScheduler(runtime, runner=fake_runner, monotonic=lambda: 0)
    state = {"last_sync": now.isoformat()}
    assert service._reports("seasonal", state, now, 180, lambda: None) is None
    fake_runner.run_scheduled.assert_not_called()
    analysis = runtime.analysis(no_ai=True)
    daily = analysis.analyze_daily(first.date(), use_ai=False, persist=False)
    daily.generated_at = now
    db.save_report(daily, daily.summary)
    assert service._reports("seasonal", state, now, 180, lambda: None)["status"] == "pending"
    period = fake_runner.run_scheduled.call_args.args[0]
    assert period.observed_end == datetime(2026, 10, 19, tzinfo=UTC)
    report = analysis.analyze_period(period, use_ai=False)
    report.context["schedule_signature"] = schedule_signature(analysis, period)
    report.context["scheduler_daily_signature"] = seasonal_daily_signature(analysis, period)
    db.save_report(report, report.summary)
    assert not scheduler.period_needs_report(analysis, period)
    daily.summary = "Changed interpretation alone is not new evidence"
    db.save_report(daily, daily.summary)
    assert not scheduler.period_needs_report(analysis, period)
    daily.quality.score = 0.5
    db.save_report(daily, daily.summary)
    assert scheduler.period_needs_report(analysis, period)


@pytest.mark.ydb
@pytest.mark.parametrize("interrupted_completion", [False, True])
def test_season_job_reopens_when_daily_repair_follows_telemetry_refresh(
    tmp_path: Path, monkeypatch, interrupted_completion: bool,
) -> None:
    db, runner, _client = _runner(tmp_path)
    runner.now = lambda: datetime(2026, 10, 20, tzinfo=UTC)
    analysis = runner.runtime.analysis(no_ai=True)
    period = analysis.seasonal_period(2026, "autumn", as_of=datetime(2026, 10, 19, tzinfo=UTC))
    _coverage(db, period)
    daily_date = datetime(2026, 9, 22, tzinfo=UTC)
    analysis.analyze_daily(daily_date.date(), use_ai=False)
    # Telemetry arrives first. A seasonal refresh can happen before the daily
    # repair, so it observes new source revisions but old daily aggregate facts.
    point = TelemetryPoint(device_id="fixture", source_type="temperature", entity_id="room",
                           metric_key="temperature", timestamp_utc=daily_date, value_num=20)
    db.telemetry.write_window(device_id="fixture", data_type="temperature", start=daily_date,
                              end=daily_date + timedelta(minutes=30), points=[point], state="complete")
    assert runner.run_scheduled(period, use_ai=False)["phase"] == "analyze"
    complete = db.jobs.complete
    if interrupted_completion:
        monkeypatch.setattr(db.jobs, "complete", lambda *_args: False)
        with pytest.raises(RuntimeError, match="ownership expired"):
            runner.run_scheduled(period, use_ai=False)
        monkeypatch.setattr(db.jobs, "complete", complete)
    else:
        assert runner.run_scheduled(period, use_ai=False)["status"] == "done"
    original = db.report(analysis.report_id_for("seasonal", period.start))
    assert original is not None
    old_daily_signature = original.context["scheduler_daily_signature"]
    sources_before, publication_before = runner._input_state()
    analysis.analyze_daily(daily_date.date(), use_ai=False)
    sources_after, publication_after = runner._input_state()
    assert sources_after == sources_before
    assert publication_after > publication_before
    assert scheduler.period_needs_report(analysis, period)
    # Both completed-job reuse and saved-checkpoint recovery must invalidate
    # the stale aggregate. Neither may return done/reused forever.
    repaired = runner.run_scheduled(period, use_ai=False)
    if repaired["status"] == "pending":
        assert repaired["phase"] == "analyze"
        repaired = runner.run_scheduled(period, use_ai=False)
    assert repaired["status"] == "done" and repaired["reused"] is False
    refreshed = db.report(original.id)
    assert refreshed.context["scheduler_daily_signature"] != old_daily_signature
    assert refreshed.context["scheduler_daily_signature"] == seasonal_daily_signature(analysis, period)
    assert not scheduler.period_needs_report(analysis, period)
    assert runner.run_scheduled(period, use_ai=False)["reused"] is True
