"""Ensure historical report reads load payloads only after bounded key selection."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.adapters.ydb.reports import ReportRepository
from zont_analyzer.application.owner_context import OwnerContextStore
from zont_analyzer.domain.models import QualityResult, Report


class _RecordingTransaction:
    def __init__(self, tx: Any, queries: list[tuple[str, int]]) -> None:
        self._tx = tx
        self._queries = queries

    def execute(self, query: str, parameters: dict[str, Any] | None = None) -> list[Any]:
        result = self._tx.execute(query, parameters)
        if query.lstrip().upper().startswith("SELECT") or " SELECT " in query.upper():
            self._queries.append((query, sum(len(rows.rows) for rows in result)))
        return result


class _RecordingDatabase:
    def __init__(self, db: Any) -> None:
        self._db = db
        self.queries: list[tuple[str, int]] = []

    def transaction(self, callback: Callable[[Any], Any]) -> Any:
        return self._db.transaction(lambda tx: callback(_RecordingTransaction(tx, self.queries)))

    def reset(self) -> None:
        self.queries.clear()


def _report(day: int, *, kind: str = "daily", complete: bool = True, report_id: str | None = None) -> Report:
    start = datetime(2026, 1, day, tzinfo=UTC)
    end = start + timedelta(days=1)
    generated = end + timedelta(minutes=1) if complete else end - timedelta(minutes=1)
    return Report(
        id=report_id or f"report-cost-{kind}-{day}",
        kind=kind,
        period_start=start,
        period_end=end,
        generated_at=generated,
        quality=QualityResult(
            score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
            implausible_jumps=0, sample_count=1,
        ),
        summary="large report payload " + ("x" * 8192),
    )


def _payload_reads(queries: list[tuple[str, int]]) -> int:
    return sum(
        count for query, count in queries
        if "SELECT ID, PAYLOAD FROM REPORTS VIEW BY_ID" in query.upper()
    )


@pytest.mark.ydb
def test_period_history_and_completed_reads_fetch_only_selected_payloads(ydb_database: Any) -> None:
    recording = _RecordingDatabase(ydb_database)
    repository = ReportRepository(recording)  # type: ignore[arg-type]
    reports = [_report(day) for day in range(1, 13)]
    for report in reports:
        repository.save_report(report, "rendered")

    recording.reset()
    prior = repository.prior_reports(datetime(2026, 1, 14, tzinfo=UTC), limit=3)
    assert [report.id for report in prior] == [reports[11].id, reports[10].id, reports[9].id]
    assert _payload_reads(recording.queries) == 3
    assert sum(count for query, count in recording.queries if "SELECT ID FROM REPORTS" in query.upper()) == 3

    recording.reset()
    completed = repository.completed_reports(datetime(2026, 1, 14, tzinfo=UTC), limit=4)
    assert {report.id for report in completed} == {report.id for report in reports[:4]}
    assert _payload_reads(recording.queries) == 4
    assert sum(count for query, count in recording.queries if "SELECT ID FROM REPORTS" in query.upper()) == 4

    recording.reset()
    selected = repository.report_for_period(reports[4].period_start, reports[4].period_end)
    assert selected == reports[4]
    assert _payload_reads(recording.queries) == 1
    period_queries = [query for query, _ in recording.queries if "SELECT ID FROM REPORTS" in query.upper()]
    assert len(period_queries) == 1
    assert "KIND IN ('INITIAL','DAILY','WEEKLY','MONTHLY','SEASONAL')" in period_queries[0].upper()
    assert "PERIOD_START=$START AND PERIOD_END=$END" in period_queries[0].upper()


@pytest.mark.ydb
def test_latest_completed_daily_start_skips_incomplete_reports_without_history_payload_scan(
    ydb_database: Any,
) -> None:
    recording = _RecordingDatabase(ydb_database)
    repository = ReportRepository(recording)  # type: ignore[arg-type]
    complete = _report(1)
    incomplete = _report(2, complete=False)
    repository.save_report(complete, "complete")
    repository.save_report(incomplete, "incomplete")

    recording.reset()
    latest = repository.latest_completed_daily_report_start(datetime(2026, 1, 5, tzinfo=UTC))
    assert latest == complete.period_start
    assert _payload_reads(recording.queries) == 2
    assert sum(count for query, count in recording.queries if "SELECT ID,PERIOD_START" in query.upper()) == 2


@pytest.mark.ydb
def test_gas_snapshot_reuses_form_inputs_only_inside_explicit_scope(ydb_database: Any, monkeypatch: Any) -> None:
    db = Database(ydb_database)  # type: ignore[arg-type]
    report = _report(1)
    db.save_report(report, "rendered")
    store = OwnerContextStore(db)
    calls = {"report": 0, "gas_state": 0, "devices": 0, "latest": 0}

    original_report = db.reports.report
    original_gas_state = db.owner.application_gas_state
    original_devices = db.owner.application_devices
    original_latest = db.latest_completed_daily_report_start

    def report_lookup(report_id: str) -> Any:
        calls["report"] += 1
        return original_report(report_id)

    def gas_state(device_id: str) -> Any:
        calls["gas_state"] += 1
        return original_gas_state(device_id)

    def devices() -> Any:
        calls["devices"] += 1
        return original_devices()

    def latest(now: datetime) -> Any:
        calls["latest"] += 1
        return original_latest(now)

    monkeypatch.setattr(db.reports, "report", report_lookup)
    monkeypatch.setattr(db.owner, "application_gas_state", gas_state)
    monkeypatch.setattr(db.owner, "application_devices", devices)
    monkeypatch.setattr(db, "latest_completed_daily_report_start", latest)

    with store.gas_snapshot():
        first = store.gas_for_report(report)
        assert store.gas(report.id) == first
        assert calls == {"report": 1, "gas_state": 1, "devices": 1, "latest": 1}
    store.gas(report.id)
    assert calls == {"report": 2, "gas_state": 2, "devices": 2, "latest": 2}
