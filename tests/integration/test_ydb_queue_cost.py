"""Publication queue reads and writes stay bounded as the archive grows."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from tests.test_cloud_publication import MemoryStorage, _activate
from tests.unit.test_incremental_publication import _record_change, _runtime
from zont_analyzer.adapters.ydb.database import Transaction
from zont_analyzer.adapters.ydb.publication import PublicationRepository
from zont_analyzer.application import publication
from zont_analyzer.application.owner_context import OwnerContextStore
from zont_analyzer.domain import TelemetryPoint


@pytest.mark.ydb
def test_pending_batch_reads_candidates_and_compact_manifest_only(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    analysis = runtime.analysis(no_ai=True)
    for day in range(18):
        analysis.analyze_daily(date(2026, 8, 1) + timedelta(days=day), use_ai=False)
    original_tx = Transaction.execute
    saves = 0

    def count_saves(self, query, parameters=None):
        nonlocal saves
        if "UPSERT INTO publication_items SELECT * FROM AS_TABLE($rows)" in query:
            saves += 1
        return original_tx(self, query, parameters)

    monkeypatch.setattr(Transaction, "execute", count_saves)
    first = publication.publish_reports(runtime, batch_size=2)
    assert first["pending_reports"] == 16
    assert saves == 1

    original = runtime.db.storage.execute
    queries: list[str] = []

    def capture(query, parameters=None, **kwargs):
        if "publication_items" in query:
            queries.append(query)
        return original(query, parameters, **kwargs)

    monkeypatch.setattr(runtime.db.storage, "execute", capture)
    monkeypatch.setattr(PublicationRepository, "load", lambda *_: pytest.fail("full pending-index load"))
    second = publication.publish_reports(runtime, batch_size=2)
    assert second["rendered_reports"] == 2
    assert second["pending_reports"] == 14
    full_rows = [query for query in queries if "SELECT * FROM publication_items" in query]
    assert full_rows and all("LIMIT $limit" in query or "href=$href" in query for query in full_rows)
    assert any("SELECT href,entry FROM publication_items" in query for query in queries)


@pytest.mark.ydb
def test_unpublished_counter_skips_archive_scan_and_recovers_when_missing(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    analysis = runtime.analysis(no_ai=True)
    for day in range(6):
        analysis.analyze_daily(date(2026, 8, 1) + timedelta(days=day), use_ai=False)
    repository = PublicationRepository(runtime.db.storage)
    result = publication.publish_reports(runtime, batch_size=1)
    assert repository.load_meta()["unpublished_count"] == "5"
    while result["pending_reports"]:
        result = publication.publish_reports(runtime, batch_size=2)
    assert repository.load_meta()["unpublished_count"] == "0"

    _record_change(runtime.db, "global", "gas")
    assert publication.publish_reports(runtime, batch_size=1)["pending_reports"] == 5
    original_execute = runtime.db.storage.execute

    def no_unpublished_scan(query, parameters=None, **kwargs):
        assert not ("publication_items VIEW by_kind_start" in query and "entry IS NULL" in query)
        return original_execute(query, parameters, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(runtime.db.storage, "execute", no_unpublished_scan)
        assert publication.publish_reports(runtime, batch_size=1)["pending_reports"] == 4

    runtime.db.storage.execute("DELETE FROM metadata WHERE name='publication:unpublished_count';")
    original_load = PublicationRepository.load
    loads = 0

    def count_full_load(self):
        nonlocal loads
        loads += 1
        return original_load(self)

    with monkeypatch.context() as patch:
        patch.setattr(PublicationRepository, "load", count_full_load)
        publication.publish_reports(runtime, batch_size=1)
    assert loads == 1
    assert repository.load_meta()["unpublished_count"] == "0"


@pytest.mark.ydb
def test_legacy_checkpoint_and_count_change_recounts_unpublished_entries(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    analysis = runtime.analysis(no_ai=True)
    for day in range(3):
        analysis.analyze_daily(date(2026, 8, 1) + timedelta(days=day), use_ai=False)
    publication.publish_reports(runtime)
    for day in range(3, 6):
        analysis.analyze_daily(date(2026, 8, 1) + timedelta(days=day), use_ai=False)
    publication.publish_reports(runtime, batch_size=1)
    repository = PublicationRepository(runtime.db.storage)
    assert repository.load_meta()["unpublished_count"] == "2"

    # Simulate a rollback writer consuming a source change and replacing its
    # own count while leaving the new counter and its basis untouched.
    _record_change(runtime.db, "global", "cost")
    for name, value in (("checkpoint", str(runtime.db.source_revision())),
                        ("count", "99"), ("unpublished_count", "0")):
        runtime.db.storage.execute(
            "DECLARE $name AS Utf8; DECLARE $value AS Utf8; "
            "UPSERT INTO metadata (name,value) VALUES ($name,$value);",
            {"$name": "publication:" + name, "$value": value},
        )
    original_load = PublicationRepository.load
    loads = 0

    def count_full_load(self):
        nonlocal loads
        loads += 1
        return original_load(self)

    with monkeypatch.context() as patch:
        patch.setattr(PublicationRepository, "load", count_full_load)
        result = publication.publish_reports(runtime, batch_size=1)
    meta = repository.load_meta()
    assert loads == 1
    assert result["rendered_reports"] == 1
    assert meta["count"] == "5"
    assert meta["unpublished_count"] == "1"
    assert json.loads(meta["unpublished_basis"]) == [meta["checkpoint"], meta["count"]]


@pytest.mark.ydb
def test_repeated_invalidation_coalesces_and_new_source_change_survives_batch(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    reports = runtime.analysis(no_ai=True)
    for day in range(4):
        reports.analyze_daily(date(2026, 8, 1) + timedelta(days=day), use_ai=False)
    publication.publish_reports(runtime)
    _record_change(runtime.db, "global", "gas")
    assert publication.publish_reports(runtime, batch_size=1)["pending_reports"] == 3

    owner = OwnerContextStore(runtime.db)
    original = publication.render_html
    changed = False

    def concurrent_write(*args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            owner.update_gas(args[0].id, {"value_m3": "42"})
        return original(*args, **kwargs)

    monkeypatch.setattr(publication, "render_html", concurrent_write)
    assert publication.publish_reports(runtime, batch_size=1)["pending_reports"] >= 2
    monkeypatch.setattr(publication, "render_html", original)
    result = publication.publish_reports(runtime, batch_size=8)
    assert result["pending_reports"] == 0
    assert publication.publish_reports(runtime)["rendered_reports"] == 0


@pytest.mark.ydb
def test_pending_failure_keeps_checkpoint_and_retries(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    analysis = runtime.analysis(no_ai=True)
    for day in range(3):
        analysis.analyze_daily(date(2026, 8, 1) + timedelta(days=day), use_ai=False)
    assert publication.publish_reports(runtime, batch_size=1)["pending_reports"] == 2
    repository = PublicationRepository(runtime.db.storage)
    checkpoint = repository.load_meta()["checkpoint"]
    original = publication.render_html
    failed = False

    def interrupt(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("interrupted render")
        return original(*args, **kwargs)

    monkeypatch.setattr(publication, "render_html", interrupt)
    with pytest.raises(OSError, match="interrupted render"):
        publication.publish_reports(runtime, batch_size=1)
    assert repository.load_meta()["checkpoint"] == checkpoint
    assert publication.publish_reports(runtime, batch_size=8)["pending_reports"] == 0


@pytest.mark.ydb
def test_cloud_pending_upload_failure_keeps_committed_index(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    analysis = runtime.analysis(no_ai=True)
    for day in range(3):
        analysis.analyze_daily(date(2026, 8, 1) + timedelta(days=day), use_ai=False)
    storage = MemoryStorage()
    _activate(monkeypatch, storage)
    assert publication.publish_reports(runtime, batch_size=1)["pending_reports"] == 2
    repository = PublicationRepository(runtime.db.storage)
    before = repository.load_meta()
    storage.fail_category = "manifests"
    with pytest.raises(OSError, match="interrupted upload"):
        publication.publish_reports(runtime, batch_size=1)
    after = repository.load_meta()
    assert after["checkpoint"] == before["checkpoint"]
    assert after["manifest_key"] == before["manifest_key"]
    assert publication.publish_reports(runtime, batch_size=8)["pending_reports"] == 0


@pytest.mark.ydb
def test_boiler_change_after_last_meter_day_refreshes_only_its_report(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    analysis = runtime.analysis(no_ai=True)
    reports = [analysis.analyze_daily(date(2026, 8, 1) + timedelta(days=day), use_ai=False)
               for day in range(4)]
    owner = OwnerContextStore(runtime.db)
    owner.update_gas(reports[0].id, {"value_m3": "100"})
    owner.update_gas(reports[1].id, {"value_m3": "102"})
    at = reports[2].period_start + timedelta(hours=12)
    point = TelemetryPoint(
        device_id="device", source_type="z3k_boiler_adapter", entity_id="boiler",
        metric_key="rml", timestamp_utc=at, value_num=30.0,
    )
    runtime.db.telemetry.write_window(device_id="device", data_type="history", start=at,
                                      end=at + timedelta(hours=1), points=[point])
    publication.publish_reports(runtime)

    runtime.db.telemetry.write_window(
        device_id="device", data_type="history", start=at, end=at + timedelta(hours=1),
        points=[point.model_copy(update={"value_num": 40.0})],
    )
    monkeypatch.setattr(PublicationRepository, "load", lambda *_: pytest.fail("full hourly index load"))
    result = publication.publish_reports(runtime, batch_size=1)
    assert result["rendered_reports"] == 1
    assert result["pending_reports"] == 0


@pytest.mark.ydb
def test_hourly_change_outside_archive_advances_checkpoint_without_manifest_read(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 1), use_ai=False)
    outside = date(2026, 9, 1)
    timestamp = datetime(outside.year, outside.month, outside.day, 12, tzinfo=UTC)
    point = TelemetryPoint(
        device_id="device", source_type="temperature", entity_id="room",
        metric_key="temperature", timestamp_utc=timestamp, value_num=20.0,
    )
    runtime.db.telemetry.write_window(device_id="device", data_type="history", start=timestamp,
                                      end=timestamp + timedelta(hours=1), points=[point])
    publication.publish_reports(runtime)
    repository = PublicationRepository(runtime.db.storage)
    before = repository.load_meta()["checkpoint"]
    runtime.db.telemetry.write_window(
        device_id="device", data_type="history", start=timestamp,
        end=timestamp + timedelta(hours=1), points=[point.model_copy(update={"value_num": 21.0})],
    )
    monkeypatch.setattr(PublicationRepository, "load", lambda *_: pytest.fail("full hourly index load"))
    monkeypatch.setattr(PublicationRepository, "manifest_entries", lambda *_: pytest.fail("manifest scan"))
    result = publication.publish_reports(runtime)
    assert result["rendered_reports"] == 0
    after = repository.load_meta()
    assert int(after["checkpoint"]) > int(before)
    assert json.loads(after["unpublished_basis"]) == [after["checkpoint"], after["count"]]
