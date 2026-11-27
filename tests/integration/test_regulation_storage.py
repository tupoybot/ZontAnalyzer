from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.regulation_projection_support import initialize_projection
from tests.unit.test_regulation import START, inputs
from tests.ydb_support import make_database
from zont_analyzer.adapters.ydb.telemetry import bump_revision, encode
from zont_analyzer.adapters.ydb.telemetry_fences import WINDOW_PREFIX, hour_bounds
from zont_analyzer.analytics.regulation import build_regulation_facts
from zont_analyzer.application.regulation_context import RegulationFactStore
from zont_analyzer.application.regulation_reliability import apply_reliability_context
from zont_analyzer.domain import SourceEvent, TelemetryPoint

pytestmark = pytest.mark.ydb


def test_repeat_reads_compact_cache_and_late_revision_rebuilds_only_requested_day(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    store = RegulationFactStore(db)
    first = build_regulation_facts(start=START, end=START + timedelta(hours=1), inputs=inputs())
    second_start = START + timedelta(days=1)
    second = {**first, "period_start": second_start.isoformat(),
              "period_end": (second_start + timedelta(hours=1)).isoformat()}
    assert store.save(first, "day-one")
    assert store.save(second, "day-two")
    assert not store.save(first, "day-one")
    builds = []

    def build() -> dict:
        builds.append(START)
        return first

    cached, hit = store.get_or_build(scope=first["scope"], start=START, source_fingerprint="day-one",
                                    settings_version="unknown", build=build)
    assert hit and not builds
    assert cached["metrics"] == first["metrics"]
    _rebuilt, hit = store.get_or_build(scope=first["scope"], start=START, source_fingerprint="late-day-one",
                                      settings_version="unknown", build=build)
    assert not hit and builds == [START]
    assert store.cached(first["scope"], second_start, "day-two", "unknown") is not None
    assert store.cached(first["scope"], START, "day-one", "unknown") is None
    assert store.cached(first["scope"], START, "late-day-one", "unknown") is not None


def test_compact_upload_is_fenced_and_idempotent(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    store = RegulationFactStore(db)
    first = build_regulation_facts(start=START, end=START + timedelta(hours=1), inputs=inputs())
    fence = store.repository.source_fence(start=START, end=START + timedelta(days=1), device_ids=("device",))
    assert store.save(first, "source", source_fence=fence)
    assert not store.save(first, "source", source_fence=fence)
    db.storage.execute("UPSERT INTO publication_changes(scope,identifier,revision) "
                       "VALUES ('telemetry','2025-01-03T00',1);")
    assert not store.save(first, "source", source_fence=fence)
    db.storage.execute("UPSERT INTO publication_changes(scope,identifier,revision) "
                       "VALUES ('telemetry','2025-01-01T00',2);")
    with pytest.raises(ValueError, match="source changed"):
        store.save(first, "changed", source_fence=fence)
    assert store.cached(first["scope"], START, "source", "unknown") is not None
    revised_fence = store.repository.source_fence(start=START, end=START + timedelta(days=1), device_ids=("device",))
    db.storage.transaction(lambda tx: bump_revision(tx, "series:device", publish=False))
    with pytest.raises(ValueError, match="source changed"):
        store.save(first, "changed", source_fence=revised_fence)


@pytest.mark.parametrize("minute", [0, 15, 30, 45])
def test_fence_ignores_adjoining_sync_but_detects_exact_late_inputs(tmp_path: Path, minute: int) -> None:
    db = make_database(tmp_path)
    store = RegulationFactStore(db)
    repository = store.repository

    def write(at: datetime, value: float = 20) -> None:
        db.telemetry.write_window(
            device_id="device", data_type="history", start=at, end=at + timedelta(hours=1),
            points=[TelemetryPoint(device_id="device", source_type="z3k_heating_circuit", entity_id="circuit",
                                   metric_key="target_temp", timestamp_utc=at, value_num=value)], events=[],
        )

    # Local UTC+4 day: six-hour predecessor and 61-second endpoint lookahead.
    left = START + timedelta(hours=14, minutes=minute)
    midnight = START + timedelta(days=1, hours=20, minutes=minute)
    right = midnight + timedelta(seconds=61)
    write(START + timedelta(hours=1))  # Establish the series before taking fences.
    fence = repository.source_fence(start=left, end=right, device_ids=("device",))
    window_key, = [key for key in fence if key.startswith(WINDOW_PREFIX)]
    assert fence[window_key] == 0
    write(right)  # +61s is exactly the half-open endpoint.
    write(midnight + timedelta(minutes=4))  # Normal ingestion in the same UTC hour.
    write(left - timedelta(seconds=1))
    assert repository.read_fence(list(fence)) == fence
    facts = build_regulation_facts(start=START, end=START + timedelta(hours=1), inputs=inputs())
    assert store.save(facts, "outside-window", source_fence=fence)
    write(left + timedelta(hours=2))
    inside = repository.read_fence(list(fence))
    assert inside[window_key] > fence[window_key]
    write(left)  # First late arrival in an earlier, previously absent hour.
    late = repository.read_fence(list(fence))
    assert late[window_key] > inside[window_key]
    write(midnight + timedelta(seconds=60))
    endpoint = repository.read_fence(list(fence))
    assert endpoint[window_key] > late[window_key]
    with pytest.raises(ValueError, match="source changed"):
        store.save(facts, "edited-boundary", source_fence=late)
    db.storage.execute(
        "DECLARE $value AS Utf8; UPSERT INTO metadata(name,value) VALUES ('publication:checkpoint',$value);",
        {"$value": str(db.source_revision())},
    )
    write(right, 21)
    assert repository.read_fence(list(fence)) == endpoint
    write(left, 21)  # An old hour's newer global revision must become the max.
    assert repository.read_fence(list(fence))[window_key] > endpoint[window_key]


def test_precise_fence_checks_old_and_new_timestamps_when_events_move_across_end(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    repository = RegulationFactStore(db).repository
    end = START + timedelta(hours=1, seconds=61)

    def event(at: datetime) -> None:
        db.telemetry.write_window(
            device_id="device", data_type="event", start=START, end=end + timedelta(minutes=5),
            events=[SourceEvent(id="fixture-event", device_id="device", event_type="PowerOn", timestamp_utc=at)],
        )

    fence = repository.source_fence(start=START, end=end)
    event(end + timedelta(seconds=1))
    assert repository.read_fence(list(fence)) == fence
    event(end - timedelta(seconds=1))
    inside = repository.read_fence(list(fence))
    assert inside != fence
    event(end)  # Removing the prior timestamp still changes the interior.
    moved = repository.read_fence(list(fence))
    assert moved != inside
    event(end + timedelta(minutes=1))
    assert repository.read_fence(list(fence)) == moved


def test_new_precise_fence_retains_legacy_boundary_revision(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    repository = RegulationFactStore(db).repository
    edge = START + timedelta(hours=1)
    # Old writers only stored the latest full-hour revision.
    db.storage.execute(
        "UPSERT INTO revisions(scope,revision) VALUES ('publication',7);"
        "UPSERT INTO publication_changes(scope,identifier,revision,payload) VALUES "
        "('telemetry','2025-01-01T01',7,'{}');",
    )
    precise = repository.source_fence(start=START, end=edge + timedelta(seconds=61))
    coarse = repository.source_fence(start=START + timedelta(minutes=7), end=edge + timedelta(seconds=61))
    assert next(iter(precise)).startswith(WINDOW_PREFIX) and list(precise.values()) == [7]
    assert next(iter(coarse)).startswith("meta:telemetry-hours:") and list(coarse.values()) == [7]
    at = edge + timedelta(minutes=4)
    db.telemetry.write_window(
        device_id="device", data_type="history", start=at, end=at + timedelta(minutes=1),
        points=[TelemetryPoint(device_id="device", source_type="z3k_heating_circuit", entity_id="circuit",
                               metric_key="target_temp", timestamp_utc=at, value_num=20)],
    )
    assert repository.read_fence(list(precise)) == precise
    assert next(iter(repository.read_fence(list(coarse)).values())) > 7


def test_many_hour_fences_share_one_bounded_range_read_and_preserve_legacy_markers(tmp_path, monkeypatch):
    db = make_database(tmp_path)
    repository = RegulationFactStore(db).repository
    db.storage.execute(
        "UPSERT INTO publication_changes(scope,identifier,revision) VALUES "
        "('telemetry','2025-01-01T00',7),('telemetry','2025-01-01T01',19),"
        "('telemetry','2025-01-01T02',11);"
        "UPSERT INTO app_meta(key,value) VALUES ('telemetry-day:2025-01-01','legacy');",
    )
    windows = [(START, START + timedelta(hours=2)),
               (START + timedelta(hours=2), START + timedelta(hours=3)),
               (START + timedelta(hours=3), START + timedelta(hours=4))]
    execute = db.storage.execute
    queries = []

    def capture(query, parameters=None, **kwargs):
        queries.append(query)
        return execute(query, parameters, **kwargs)

    monkeypatch.setattr(db.storage, "execute", capture)
    fences = repository.source_fences(windows)
    assert [next(iter(fence.values())) for fence in fences.values()] == [19, 11, 0]
    assert len(queries) == 1 and queries[0].count("FROM publication_changes") == 1
    legacy = {"meta:telemetry-day:2025-01-01": "legacy"}
    assert repository.read_fence(list(legacy)) == legacy
    with pytest.raises(ValueError, match="10000 hours"):
        repository.read_fence(["meta:telemetry-hours:2020-01-01T00/2025-01-01T00"])


def test_sparse_hour_lineage_checks_both_ranges_on_save(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    store = RegulationFactStore(db)
    early = "meta:telemetry-hours:2024-01-01T00/2024-01-01T02"
    recent = "meta:telemetry-hours:2025-06-01T00/2025-06-01T02"
    db.storage.execute(
        "UPSERT INTO publication_changes(scope,identifier,revision) VALUES "
        "('telemetry','2024-01-01T01',7),('telemetry','2025-01-01T00',19);"
    )
    fence = store.repository.read_fence([recent, early])
    assert fence == {early: 7, recent: 0}
    facts = build_regulation_facts(start=START, end=START + timedelta(hours=1), inputs=inputs())
    assert store.save(facts, "source", source_fence=fence)
    db.storage.execute(
        "UPSERT INTO publication_changes(scope,identifier,revision) VALUES ('telemetry','2025-06-01T00',20);"
    )
    with pytest.raises(ValueError, match="source changed"):
        store.save(facts, "changed", source_fence=fence)


def test_long_hour_lineage_keeps_overlapping_ranges_complete(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    repository = RegulationFactStore(db).repository
    starts = [START + timedelta(days=day) for day in range(420)]
    keys = [f"meta:telemetry-hours:{start:%Y-%m-%dT%H}/{start + timedelta(hours=31):%Y-%m-%dT%H}"
            for start in starts]
    db.storage.execute(
        "DECLARE $overlap AS Utf8; DECLARE $later AS Utf8; "
        "UPSERT INTO publication_changes(scope,identifier,revision) VALUES "
        "('telemetry',$overlap,11),('telemetry',$later,21);",
        {"$overlap": f"{starts[416]:%Y-%m-%d}T01", "$later": f"{starts[417]:%Y-%m-%d}T01"},
    )
    fence = repository.read_fence(keys)
    assert [fence[key] for key in keys[414:]] == [0, 11, 21, 21, 0, 0]
    assert len(fence) == len(keys)


@pytest.mark.parametrize("marker", ["precise", "hour", "day"])
def test_saved_regulation_days_keep_legacy_or_precise_dependencies(tmp_path, monkeypatch, marker):
    from tests.unit.test_regulation_report_cache import CacheWorld
    from zont_analyzer.adapters.ydb.regulation import RegulationRepository
    from zont_analyzer.application import regulation_reports as reports

    db = make_database(tmp_path)
    world = CacheWorld()
    selected = SimpleNamespace(storage=db.storage, list_devices=world.list_devices, list_series=world.list_series,
                               control_settings_history=world.control_settings_history)
    original_keys = RegulationRepository._fence_keys

    def legacy_keys(start, end, *, device_ids):
        keys = [key for key in original_keys(start, end, device_ids=device_ids)
                if not key.startswith(("meta:telemetry-hours:", WINDOW_PREFIX))]
        if marker == "hour":
            left, right = hour_bounds(start, end)
            return [*keys, f"meta:telemetry-hours:{left:%Y-%m-%dT%H}/{right:%Y-%m-%dT%H}"]
        day = start.astimezone(UTC).date()
        while day <= (end - timedelta(microseconds=1)).astimezone(UTC).date():
            keys.append("meta:telemetry-day:" + day.isoformat())
            day += timedelta(days=1)
        return keys

    def prepare(*args, **kwargs):
        data, selection, fence = world.prepare(*args, **kwargs)
        return replace(data, reliability_context={}), selection, fence

    def sync(at: datetime) -> None:
        db.telemetry.write_window(
            device_id="device", data_type="history", start=at, end=at + timedelta(minutes=1),
            points=[TelemetryPoint(device_id="device", source_type="z3k_heating_circuit", entity_id="circuit",
                                   metric_key="target_temp", timestamp_utc=at, value_num=20)],
        )

    monkeypatch.setattr(reports, "regulation_scopes", lambda *_: [("device", "circuit")])
    monkeypatch.setattr(reports, "regulation_source_selections", lambda *_: {
        "device/circuit": {"version": "synthetic-selection"}})
    monkeypatch.setattr(reports, "prepare_regulation_day", prepare)
    if marker != "precise":
        monkeypatch.setattr(RegulationRepository, "_fence_keys", staticmethod(legacy_keys))
    left, right = START + timedelta(days=2), START + timedelta(days=3)
    first = reports.validated_regulation_days(selected, left, right, timezone="UTC", calculate_daily=True)
    assert first["built_days"] == 2
    store = RegulationFactStore(selected)
    saved = store.read("device/circuit", left - timedelta(days=1), right)
    monkeypatch.setattr(RegulationRepository, "_fence_keys", staticmethod(original_keys))
    if marker == "precise":
        sync(right + timedelta(minutes=5))
    repeated = reports.validated_regulation_days(selected, left, right, timezone="UTC")
    assert repeated["hit_days"] == 2 and repeated["built_days"] == 0
    assert repeated["dependency_end"] == first["dependency_end"]  # Keep evidence bounds stable.
    assert store.read("device/circuit", left - timedelta(days=1), right) == saved
    assert len(world.raw_reads) == 2
    sync(right + timedelta(seconds=60))
    stale = reports.validated_regulation_days(selected, left, right, timezone="UTC")
    assert stale["circuits"][0]["facts"] == [] and stale["built_days"] == 0


def test_sixty_day_repeat_uses_one_compact_range_query_without_telemetry_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = make_database(tmp_path)
    store = RegulationFactStore(db)
    fact = build_regulation_facts(start=START, end=START + timedelta(days=1), inputs=inputs(minutes=1440))
    stored_bytes = 0
    for index in range(60):
        day = deepcopy(fact)
        start, end = START + timedelta(days=index), START + timedelta(days=index + 1)
        day["period_start"], day["period_end"] = start.isoformat(), end.isoformat()
        day["runs"][0]["start"], day["runs"][0]["end"] = start.isoformat(), end.isoformat()
        stored_bytes += len(encode(day).encode())
        store.save(day, str(index))
    queries = []
    original = db.storage.execute

    def counted(query: str, *args: object, **kwargs: object) -> object:
        queries.append(query)
        return original(query, *args, **kwargs)

    monkeypatch.setattr(db.storage, "execute", counted)
    for _repeat in range(2):
        before = len(queries)
        report = store.aggregate(fact["scope"], START, START + timedelta(days=60), expected_days=60)
        assert report["included_days"] == 60
        assert report["coverage"]["eligible_seconds"] == 60 * 86400
        assert len(queries) - before == 1
    assert all("regulation_day_facts" in query and "telemetry_samples" not in query for query in queries)
    assert stored_bytes < 1_000_000
    print({"days": 60, "cached_range_queries": len(queries), "stored_fact_bytes": stored_bytes})


def test_source_event_window_is_bounded_and_explicit_loss_overrides_valid_room_values(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    events = [SourceEvent(id="loss", device_id="device", event_type="LossConnectionBoiler",
                          timestamp_utc=START + timedelta(minutes=10)),
              SourceEvent(id="restore", device_id="device", event_type="ReconnectingBoiler",
                          timestamp_utc=START + timedelta(minutes=20))]
    db.telemetry.write_window(device_id="device", data_type="events", start=START,
                              end=START + timedelta(hours=1), points=[], events=events)
    initialize_projection(db, tmp_path)
    store = RegulationFactStore(db)
    context = store.repository.source_events("device", START, START + timedelta(hours=1))
    assert not context["incomplete"] and len(context["events"]) == 2
    data = apply_reliability_context(inputs(), context, start=START, end=START + timedelta(hours=1))
    facts = build_regulation_facts(start=START, end=START + timedelta(hours=1), inputs=data)
    assert facts["coverage"]["eligible_seconds"] == 47 * 60
    assert facts["coverage"]["exclusion_seconds"]["recovery:reliability"] == 13 * 60


def test_missing_projection_is_explicit_then_complete_index_proves_absent_families(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    start = START - timedelta(days=1)
    events = [SourceEvent(id=f"prior:{index}", device_id="device", event_type="Info",
                          timestamp_utc=start + timedelta(seconds=index)) for index in range(140)]
    db.telemetry.write_window(device_id="device", data_type="events", start=start, end=START,
                              points=[], events=events)
    context = RegulationFactStore(db).repository.source_events("device", START, START + timedelta(days=1))
    assert context["incomplete"]
    assert not context["projection_ready"]
    initialize_projection(db, tmp_path)
    complete = RegulationFactStore(db).repository.source_events("device", START, START + timedelta(days=1))
    assert not complete["incomplete"] and complete["events"] == []
    restores = [SourceEvent(id=f"last:{kind}", device_id="device", event_type=kind,
                            timestamp_utc=START - timedelta(minutes=5 - index))
                for index, kind in enumerate(("ReconnectingBoiler", "MainPowerFound", "PowerOn", "connected"))]
    db.telemetry.write_window(device_id="device", data_type="events", start=start, end=START,
                              points=[], events=restores)
    resolved = RegulationFactStore(db).repository.source_events("device", START, START + timedelta(days=1))
    assert resolved["prior_state_complete"]
    assert not resolved["incomplete"]


@pytest.mark.parametrize("kind", ["setpoint", "event"])
def test_first_arrival_markers_guard_absent_history_without_invalidating_on_future_inputs(
    tmp_path: Path, kind: str,
) -> None:
    db = make_database(tmp_path)

    def write(at: datetime) -> None:
        points = [TelemetryPoint(device_id="device", source_type="z3k_heating_circuit", entity_id="circuit",
                                  metric_key="target_temp", timestamp_utc=at,
                                  value_num=20)] if kind == "setpoint" else []
        events = [SourceEvent(id=str(at), device_id="device", event_type="PowerOn", timestamp_utc=at)
                  ] if kind == "event" else []
        db.telemetry.write_window(device_id="device", data_type="history" if points else "events",
                                  start=at, end=at + timedelta(hours=1), points=points, events=events)

    write(START + timedelta(days=100))
    key = (f"regulation-first-setpoint:{db.list_series()[0]['id']}" if kind == "setpoint" else
           "regulation-first-event:device:controller")
    data = replace(inputs(), negative_input_guards=((key, int(START.timestamp())),))
    facts = build_regulation_facts(start=START, end=START + timedelta(hours=1), inputs=data)
    store = RegulationFactStore(db)
    assert store.save(facts, "future-does-not-affect-absence")
    write(START - timedelta(days=10))
    with pytest.raises(ValueError, match="previously absent"):
        store.save(facts, "late-first-historical-input")


def test_offline_publication_is_atomic_coalesced_and_normal_analysis_does_not_self_invalidate(tmp_path: Path) -> None:
    import json

    db = make_database(tmp_path)
    store = RegulationFactStore(db)
    fact = build_regulation_facts(start=START, end=START + timedelta(hours=1), inputs=inputs())
    before = db.source_revision()
    assert store.save(fact, "normal")
    assert db.source_revision() == before
    assert store.save(fact, "offline", publish=True)
    published = db.source_revision()
    assert not store.save(fact, "offline", publish=True)
    assert db.source_revision() == published
    following = {**fact, "period_start": (START + timedelta(days=1)).isoformat(),
                 "period_end": (START + timedelta(days=1, hours=1)).isoformat()}
    assert store.save(following, "offline-next-day", publish=True)
    row, = db.storage.execute("SELECT payload FROM publication_changes WHERE scope='regulation-facts';")[0].rows
    assert json.loads(row.payload) == {"change_start": int(START.timestamp() * 1_000_000),
                                       "change_end": int((START + timedelta(days=1, hours=1)).timestamp() * 1_000_000)}
