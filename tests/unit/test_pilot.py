from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from zont_analyzer.application.pilot import (
    PilotService,
    WorkerCycleError,
    atomic_write_text,
    read_worker_status,
    reports_directory,
    worker_health,
)
from zont_analyzer.config import AppConfig, PilotConfig
from zont_analyzer.domain import QualityResult, Report


def _report(selected: date) -> Report:
    start = datetime.combine(selected, time.min, UTC)
    return Report(
        id=f"report:daily:{int(start.timestamp())}:report-v1",
        kind="daily",
        period_start=start,
        period_end=start + timedelta(days=1),
        generated_at=datetime.now(UTC),
        quality=QualityResult(
            score=1,
            coverage_pct=100,
            max_gap_seconds=0,
            stuck_pct=0,
            implausible_jumps=0,
            sample_count=1,
        ),
        summary=f"Report for {selected.isoformat()}",
        context={"recommendation_policy": "p2-1.7"},
    )


class FakeDatabase:
    @staticmethod
    def period_data_revision(_start: datetime, _end: datetime) -> str:
        import hashlib

        return hashlib.sha256(b"[]").hexdigest()

    def __init__(self) -> None:
        self.reports: dict[str, Report] = {}
        self.path = Path("/tmp/zont-analyzer-fake.sqlite3")

    def list_devices(self) -> list[dict[str, Any]]:
        return []

    def list_series(self) -> list[dict[str, Any]]:
        return []

    def earliest_sample_time(self) -> datetime:
        return datetime(2026, 8, 1, 12, tzinfo=UTC)

    def report(self, report_id: str) -> Report | None:
        return self.reports.get(report_id)

    def completed_reports(self, now: datetime) -> list[Report]:
        return [report for report in self.reports.values() if report.period_end <= now]

    def save_report(self, report: Report, _rendered_text: str) -> None:
        self.reports[report.id] = report

    def recommendation_views_for_report(self, _report_id: str) -> dict[str, dict[str, Any]]:
        return {}

    def flush_log_outbox(self) -> list[str]:
        return ["report notification"]


@pytest.fixture(autouse=True)
def fake_owner_store(monkeypatch):
    from zont_analyzer.application.gas_tariffs import GasTariffStore
    from zont_analyzer.application.owner_context import OwnerContextStore

    # Calendar scheduling has separate real-DB integration coverage.
    monkeypatch.setattr("zont_analyzer.application.period_schedule.run_period_schedule", lambda *args: [])
    from unittest.mock import Mock

    from zont_analyzer.application.gas import GasService

    # Gas recalibration has real-DB integration coverage; this fixture isolates scheduling.
    original_gas_service = GasService
    def gas_service(db, config):
        if isinstance(db, FakeDatabase):
            return Mock(refresh=lambda report: report, persist_refresh=lambda old, new: True)
        return original_gas_service(db, config)
    monkeypatch.setattr("zont_analyzer.application.gas.GasService", gas_service)
    original = OwnerContextStore.gas

    def gas(store, report_id):
        if isinstance(store.db, FakeDatabase):
            return {"report_id": report_id, "reading": None, "audit": []}
        return original(store, report_id)

    monkeypatch.setattr(OwnerContextStore, "gas", gas)

    original_tariff_history = GasTariffStore.history

    def tariff_history(store, scope="installation"):
        if isinstance(store.db, FakeDatabase):
            return []
        return original_tariff_history(store, scope)

    monkeypatch.setattr(GasTariffStore, "history", tariff_history)


class FakeAnalysis:
    def __init__(self, db: FakeDatabase) -> None:
        self.db = db
        self.calls: list[tuple[date, bool]] = []

    def local_today(self) -> date:
        return date(2026, 8, 4)

    def local_day_window(self, selected: date) -> tuple[datetime, datetime]:
        start = datetime.combine(selected, time.min, UTC)
        return start, start + timedelta(days=1)

    @staticmethod
    def report_id_for(kind: str, start: datetime) -> str:
        return f"report:{kind}:{int(start.timestamp())}:report-v1"

    def analyze_daily(self, selected: date, *, use_ai: bool = True) -> Report:
        self.calls.append((selected, use_ai))
        report = _report(selected)
        self.db.reports[report.id] = report
        return report


class FakeIngestion:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result

    def sync(self) -> dict[str, Any]:
        return self.result


class FakeRuntime:
    def __init__(self, data_dir: Path, sync_result: dict[str, Any]) -> None:
        self.loaded = SimpleNamespace(data_dir=data_dir)
        self.config = AppConfig(
            pilot=PilotConfig(
                reports_dir="published",
                worker_status_file="state/worker.json",
                max_catchup_days=30,
            )
        )
        self.db = FakeDatabase()
        self.analysis_service = FakeAnalysis(self.db)
        self.sync_result = sync_result

    def zont_client(self) -> nullcontext[object]:
        return nullcontext(object())

    def ingestion(self, _client: object) -> FakeIngestion:
        return FakeIngestion(self.sync_result)

    def analysis(self) -> FakeAnalysis:
        return self.analysis_service

    @staticmethod
    def maintain_recommendation_lifecycle(*, now: datetime | None = None) -> dict[str, Any]:
        return {
            "cutoff": (now or datetime.now(UTC)).isoformat(),
            "eligible": 0,
            "ignored": 0,
            "status_counts": {"new": 0, "applied": 0, "rejected": 0, "ignored": 0},
        }


def test_pilot_catches_up_recomputes_yesterday_and_publishes_atomically(tmp_path: Path) -> None:
    runtime = FakeRuntime(tmp_path, {"complete": True, "samples": 4, "errors": []})
    existing_first = _report(date(2026, 8, 1))
    existing_yesterday = _report(date(2026, 8, 3))
    runtime.db.reports[existing_first.id] = existing_first
    runtime.db.reports[existing_yesterday.id] = existing_yesterday

    result = PilotService(runtime).run_cycle()  # type: ignore[arg-type]

    assert result["ok"] is True
    assert runtime.analysis_service.calls == [
        (date(2026, 8, 2), False),
        (date(2026, 8, 3), False),
    ]
    publish_dir = tmp_path / "published"
    assert (publish_dir / "latest.html").is_file()
    assert (publish_dir / "latest.html").stat().st_mode & 0o777 == 0o644
    for selected in ("2026-08-01", "2026-08-02", "2026-08-03"):
        assert (publish_dir / "daily" / f"{selected}.html").is_file()
        assert (publish_dir / "daily" / f"{selected}.json").is_file()
    assert not list(tmp_path.rglob("*.tmp"))
    status = read_worker_status(tmp_path / "state" / "worker.json")
    assert (tmp_path / "state" / "worker.json").stat().st_mode & 0o777 == 0o600
    assert status["state"] == "ok"
    assert status["latest_report_id"] == existing_yesterday.id


@pytest.mark.parametrize("legacy_policy", [False, True])
def test_ai_is_called_only_for_first_yesterday_report_and_reused_afterward(
    tmp_path: Path, legacy_policy: bool
) -> None:
    runtime = FakeRuntime(tmp_path, {"complete": True, "samples": 4, "errors": []})
    runtime.db.reports[_report(date(2026, 8, 1)).id] = _report(date(2026, 8, 1))
    runtime.db.reports[_report(date(2026, 8, 2)).id] = _report(date(2026, 8, 2))

    PilotService(runtime).run_cycle()  # type: ignore[arg-type]

    assert runtime.analysis_service.calls[-1] == (date(2026, 8, 3), True)
    yesterday_id = _report(date(2026, 8, 3)).id
    ai_report = runtime.db.reports[yesterday_id].model_copy(
        update={"ai_used": True, "summary": "AI summary"}
    )
    from zont_analyzer.domain.reasoning import Unknown
    ai_report.unknowns = [Unknown(id="unknown:presence", statement="Присутствие неизвестно")]
    if legacy_policy:
        ai_report.context.pop("recommendation_policy")
    runtime.db.reports[yesterday_id] = ai_report
    runtime.analysis_service.calls.clear()

    PilotService(runtime).run_cycle()  # type: ignore[arg-type]

    assert runtime.analysis_service.calls == [(date(2026, 8, 3), False)]
    retained = runtime.db.reports[yesterday_id]
    if legacy_policy:
        assert retained.ai_used is False
        assert retained.summary != "AI summary"
        return
    assert retained.ai_used is True
    assert retained.summary == "AI summary"
    assert retained.unknowns == ai_report.unknowns
    assert retained.context["pilot_ai_reuse"]["reason"].startswith("daily facts recomputed")


def test_partial_sync_fails_cycle_and_records_unhealthy_status(tmp_path: Path) -> None:
    runtime = FakeRuntime(
        tmp_path,
        {"complete": False, "failed_windows": 1, "errors": ["device did not respond"]},
    )

    with pytest.raises(WorkerCycleError, match="incomplete"):
        PilotService(runtime).run_cycle()  # type: ignore[arg-type]

    status_path = tmp_path / "state" / "worker.json"
    assert read_worker_status(status_path)["state"] == "error"
    assert worker_health(status_path, max_age_seconds=60)["ok"] is False
    assert runtime.analysis_service.calls == []


def test_paths_cannot_escape_data_dir_and_atomic_write_replaces_content(tmp_path: Path) -> None:
    runtime = FakeRuntime(tmp_path, {"complete": True})
    runtime.config = runtime.config.model_copy(
        update={"pilot": runtime.config.pilot.model_copy(update={"reports_dir": "../public"})}
    )
    with pytest.raises(ValueError, match="inside the data directory"):
        reports_directory(runtime)  # type: ignore[arg-type]

    target = tmp_path / "status.json"
    atomic_write_text(target, "old")
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob("*.tmp"))


def test_health_rejects_stale_status(tmp_path: Path) -> None:
    path = tmp_path / "worker.json"
    atomic_write_text(
        path,
        '{"state":"ok","updated_at":"2026-08-03T00:00:00+00:00"}\n',
    )

    result = worker_health(
        path,
        max_age_seconds=60,
        now=datetime(2026, 8, 3, 0, 2, tzinfo=UTC),
    )

    assert result["ok"] is False
    assert result["age_seconds"] == 120


def test_current_calculation_version_does_not_recompute_yesterday_on_each_poll(tmp_path: Path) -> None:
    from zont_analyzer.application.analysis import CALCULATION_VERSION

    runtime = FakeRuntime(tmp_path, {"complete": True, "samples": 4, "errors": []})
    for day in (1, 2, 3):
        report = _report(date(2026, 8, day))
        report.context["calculation_version"] = CALCULATION_VERSION
        runtime.db.reports[report.id] = report
    PilotService(runtime).run_cycle()  # type: ignore[arg-type]
    PilotService(runtime).run_cycle()  # type: ignore[arg-type]
    assert runtime.analysis_service.calls == []


def test_pilot_adopts_exact_revision_without_reanalysis_or_changing_ai(tmp_path: Path, monkeypatch) -> None:
    from zont_analyzer.application.analysis import CALCULATION_VERSION

    runtime = FakeRuntime(tmp_path, {'complete': True})
    yesterday = _report(date(2026, 8, 3))
    yesterday.ai_used = True
    yesterday.context.update(calculation_version=CALCULATION_VERSION,
                             input_revision={'telemetry': 'legacy-marker'})
    runtime.db.reports[yesterday.id] = yesterday
    monkeypatch.setattr(PilotService, '_completed_dates', lambda *args: [date(2026, 8, 3)])
    monkeypatch.setattr(runtime.db, 'period_data_revision', lambda *args: 'telemetry-v2:exact')
    monkeypatch.setattr(runtime.db, 'legacy_period_data_revision', lambda *args: 'legacy-marker', raising=False)
    upgrades = []
    monkeypatch.setattr(runtime.db, 'upgrade_report_telemetry_revision',
                        lambda *args: upgrades.append(args) or True, raising=False)
    result = PilotService(runtime).run_cycle()
    assert result['analyzed_dates'] == []
    assert runtime.analysis_service.calls == []
    assert upgrades == [(yesterday.id, 'legacy-marker', 'telemetry-v2:exact')]
    assert yesterday.ai_used and not yesterday.context.get('pilot_ai_reuse')


@pytest.mark.parametrize('revision_changed', [False, True])
def test_pilot_reanalyzes_only_changed_exact_period(tmp_path: Path, monkeypatch, revision_changed: bool) -> None:
    from zont_analyzer.application.analysis import CALCULATION_VERSION

    runtime = FakeRuntime(tmp_path, {'complete': True, 'samples': 10})
    yesterday = _report(date(2026, 8, 3))
    yesterday.ai_used = True
    yesterday.context.update(calculation_version=CALCULATION_VERSION,
                             input_revision={'telemetry': 'telemetry-v2:original'})
    runtime.db.reports[yesterday.id] = yesterday
    monkeypatch.setattr(PilotService, '_completed_dates', lambda *args: [date(2026, 8, 3)])
    # Includes the all-samples-deleted case, formerly ignored as an empty revision.
    import hashlib
    revision = hashlib.sha256(b'[]').hexdigest() if revision_changed else 'telemetry-v2:original'
    monkeypatch.setattr(runtime.db, 'period_data_revision', lambda *args: revision)
    result = PilotService(runtime).run_cycle()
    assert result['analyzed_dates'] == (['2026-08-03'] if revision_changed else [])
    assert runtime.analysis_service.calls == ([(date(2026, 8, 3), False)] if revision_changed else [])


def test_imported_history_without_revisions_is_not_reanalyzed_on_upgrade(tmp_path: Path, monkeypatch) -> None:
    import hashlib

    from zont_analyzer.application.analysis import CALCULATION_VERSION

    runtime = FakeRuntime(tmp_path, {'complete': True})
    yesterday = _report(date(2026, 8, 3))
    yesterday.context['calculation_version'] = CALCULATION_VERSION
    runtime.db.reports[yesterday.id] = yesterday
    monkeypatch.setattr(PilotService, '_completed_dates', lambda *args: [date(2026, 8, 3)])
    monkeypatch.setattr(runtime.db, 'period_data_revision', lambda *args: 'telemetry-v2:imported')
    monkeypatch.setattr(runtime.db, 'legacy_period_data_revision',
                        lambda *args: hashlib.sha256(b'[]').hexdigest(), raising=False)
    assert PilotService(runtime).run_cycle()['analyzed_dates'] == []
