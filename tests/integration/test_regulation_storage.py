from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.unit.test_regulation import START, inputs
from tests.ydb_support import make_database
from zont_analyzer.adapters.ydb.telemetry import bump_revision, encode
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
    db.storage.execute("UPSERT INTO app_meta (key,value) VALUES ('telemetry-day:2025-01-03','new-data');")
    assert not store.save(first, "source", source_fence=fence)
    db.storage.execute("UPSERT INTO app_meta (key,value) VALUES ('telemetry-day:2025-01-01','late-data');")
    with pytest.raises(ValueError, match="source changed"):
        store.save(first, "changed", source_fence=fence)
    assert store.cached(first["scope"], START, "source", "unknown") is not None
    revised_fence = store.repository.source_fence(start=START, end=START + timedelta(days=1), device_ids=("device",))
    db.storage.transaction(lambda tx: bump_revision(tx, "series:device", publish=False))
    with pytest.raises(ValueError, match="source changed"):
        store.save(first, "changed", source_fence=revised_fence)


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
    store = RegulationFactStore(db)
    context = store.repository.source_events("device", START, START + timedelta(hours=1))
    assert not context["incomplete"] and len(context["events"]) == 2
    data = apply_reliability_context(inputs(), context, start=START, end=START + timedelta(hours=1))
    facts = build_regulation_facts(start=START, end=START + timedelta(hours=1), inputs=data)
    assert facts["coverage"]["eligible_seconds"] == 47 * 60
    assert facts["coverage"]["exclusion_seconds"]["recovery:reliability"] == 13 * 60


def test_source_event_predecessor_cap_is_explicit_instead_of_scanning_lifetime(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    start = START - timedelta(days=1)
    events = [SourceEvent(id=f"prior:{index}", device_id="device", event_type="Info",
                          timestamp_utc=start + timedelta(seconds=index)) for index in range(140)]
    db.telemetry.write_window(device_id="device", data_type="events", start=start, end=START,
                              points=[], events=events)
    context = RegulationFactStore(db).repository.source_events("device", START, START + timedelta(days=1))
    assert context["incomplete"]
    assert len(context["events"]) == 128
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
