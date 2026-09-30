"""Latest report lookup over a compact, revision-checked YDB catalogue."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import ydb  # type: ignore[import-untyped]

from tests.integration.test_ydb_reports import _report
from tests.ydb_support import make_database
from zont_analyzer.adapters.ydb import latest_report as latest_module
from zont_analyzer.domain import Report


def _clear_cache(db: Any) -> None:
    rows = db.storage.execute(
        "SELECT key FROM app_meta WHERE key>='latest-report:v1:' AND key<'latest-report:v1;'"
    )[0].rows
    keys = [str(row.key) for row in rows]
    if keys:
        db.storage.execute(
            'DECLARE $keys AS List<Utf8>; DELETE FROM app_meta WHERE key IN $keys;',
            {'$keys': ydb.TypedValue(keys, ydb.ListType(ydb.PrimitiveType.Utf8))},
        )


def _dated(report_id: str, day: int, generated: datetime) -> Report:
    base = _report(report_id=report_id)
    start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day)
    return base.model_copy(deep=True, update={
        'period_start': start, 'period_end': start + timedelta(days=1),
        'generated_at': generated,
    })


@pytest.mark.ydb
def test_latest_report_uses_generated_time_and_warm_lookup_reads_one_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = make_database(tmp_path)
    generated = datetime(2026, 4, 1, tzinfo=UTC)
    early = _dated('a-earlier-period', 0, generated)
    later = _dated('z-later-period', 5, generated)
    db.save_report(later, 'later')
    db.save_report(early, 'earlier')

    _clear_cache(db)  # Simulate the unchanged pre-cache report writer.
    assert latest_module.latest_report(db).id == early.id  # type: ignore[union-attr]
    execute = db.storage.execute
    payload_reads: list[str] = []

    def track(query: str, parameters: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        if 'SELECT' in query and 'payload' in query and 'FROM reports' in query:
            payload_reads.append(query)
        return execute(query, parameters, **kwargs)

    monkeypatch.setattr(db.storage, 'execute', track)
    assert latest_module.latest_report(db).id == early.id  # type: ignore[union-attr]
    assert len(payload_reads) == 1
    assert 'WHERE id=$id' in payload_reads[0]

    # A newer generated report may cover an older reporting period.
    revised = later.model_copy(deep=True, update={'generated_at': generated + timedelta(days=1)})
    db.save_report(revised, 'new generation')
    payload_reads.clear()
    assert latest_module.latest_report(db).id == later.id  # type: ignore[union-attr]
    assert len(payload_reads) == 1  # The writer maintained the compact metadata atomically.


@pytest.mark.ydb
def test_latest_report_detects_report_changes_during_catalogue_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = make_database(tmp_path)
    first = _dated('first', 0, datetime(2026, 2, 1, tzinfo=UTC))
    db.save_report(first, 'first')
    latest_module.latest_report(db)
    original_cached = latest_module._cached

    def mutate(*args: Any, **kwargs: Any) -> Any:
        result = original_cached(*args, **kwargs)
        second = _dated('second', 1, first.generated_at + timedelta(days=1))
        db.save_report(second, 'second')
        return result

    monkeypatch.setattr(latest_module, '_cached', mutate)
    with pytest.raises(ValueError, match='reports changed'):
        latest_module.latest_report(db)
    monkeypatch.setattr(latest_module, '_cached', original_cached)
    assert latest_module.latest_report(db).id == 'second'  # type: ignore[union-attr]


@pytest.mark.ydb
def test_latest_report_pages_more_than_one_hundred_ids_without_warm_payload_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = make_database(tmp_path)
    generated = datetime(2026, 4, 1, tzinfo=UTC)
    for day in range(105):
        report = _dated(f'report-{day:03d}', day, generated + timedelta(seconds=day))
        db.save_report(report, f'rendered {day}')

    _clear_cache(db)  # One cold compatibility fill for a pre-cache archive.
    assert latest_module.latest_report(db).id == 'report-104'  # type: ignore[union-attr]
    execute = db.storage.execute
    payload_reads: list[str] = []

    def track(query: str, parameters: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        if 'SELECT' in query and 'payload' in query and 'FROM reports' in query:
            payload_reads.append(query)
        return execute(query, parameters, **kwargs)

    monkeypatch.setattr(db.storage, 'execute', track)
    assert latest_module.latest_report(db).id == 'report-104'  # type: ignore[union-attr]
    assert len(payload_reads) == 1
    assert 'WHERE id=$id' in payload_reads[0]
