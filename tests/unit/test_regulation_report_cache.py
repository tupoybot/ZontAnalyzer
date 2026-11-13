from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest

from tests.unit.test_regulation import START, inputs
from zont_analyzer.adapters.ydb.regulation import RegulationRepository
from zont_analyzer.analytics.evidence import ExclusionWindow
from zont_analyzer.analytics.settings_history import settings_timeline
from zont_analyzer.application import regulation_reports as reports


class CacheWorld:
    def __init__(self) -> None:
        self.metadata_revision = 1
        self.markers: dict[str, str] = {}
        self.facts: dict[tuple[str, str], dict[str, Any]] = {}
        self.raw_reads: list[datetime] = []
        self.writes = 0
        self.future_change = False
        self.unresolved_dhw = False
        self.held_target = False
        self.negative_input: str | None = None
        self.storage = object()

    def source_fence(self) -> dict[str, int]:
        return {"device:device": self.metadata_revision, "series:device": 1, "telemetry:device": 99}

    def source_fences(self, windows: list, *, device_ids: tuple) -> dict:
        result = {}
        for window in windows:
            keys = RegulationRepository._fence_keys(*window, device_ids=device_ids)
            result[window] = self.read_fence(keys)
        return result

    def read_fence(self, keys: list[str]) -> dict[str, int | str]:
        return {key: self.markers.get(key, "") if key.startswith("meta:") else
                self.source_fence().get(key.removeprefix("revision:"), 0) for key in keys}

    def list_devices(self) -> list:
        return [{"id": "device"}]

    def list_series(self) -> list:
        return []

    def control_settings_history(self, device: str, circuit: str, start: datetime, end: datetime) -> dict:
        state = {"status": "observed", "fields": {"type": {"status": "observed", "value": 3}}}
        rows = [{"event_id": 1, "observed_at": int((START - timedelta(days=10)).timestamp() * 1_000_000),
                 "last_confirmed_at": int((START + timedelta(days=9)).timestamp() * 1_000_000), "state": state}]
        if self.future_change:
            rows.append({"event_id": 2, "observed_at": int((START + timedelta(days=10)).timestamp() * 1_000_000),
                         "state": {"status": "observed", "fields": {"type": {"status": "observed", "value": 3},
                                                                    "pza": {"status": "observed", "value": 1}}}})
        return settings_timeline(rows, device_id=device, circuit_id=circuit, start=start, end=end)

    def read(self, scope: str, start: datetime, end: datetime) -> list:
        return [deepcopy(fact) for (stored_scope, _key), fact in sorted(self.facts.items())
                if stored_scope == scope and start <= datetime.fromisoformat(fact["period_start"]) < end]

    def save(self, fact: dict, fingerprint: str, *, source_fence: dict) -> bool:
        self.writes += 1
        self.facts[(fact["scope"], fact["period_start"])] = deepcopy(fact)
        return True

    def prepare(self, db: object, left: datetime, right: datetime, *, source_fence: dict,
                history: dict, **_kwargs: object) -> tuple:
        self.raw_reads.append(left)
        data = inputs(minutes=1800, room=[19.0 if self.unresolved_dhw else 20.0] * 1801,
                      request=[1.0 if self.unresolved_dhw else 0.0] * 1801)
        origin = left - timedelta(hours=6)
        data = replace(data, signals={key: replace(series, samples=tuple(
            replace(sample, timestamp=sample.timestamp + (origin - START)) for sample in series.samples))
            for key, series in data.signals.items()},
            settings_version=reports.day_history(history, left, right)["fingerprint"])
        if self.unresolved_dhw:
            data = replace(data, exclusions=(ExclusionWindow(START + timedelta(hours=23),
                                                            START + timedelta(hours=23, minutes=30), "dhw"),))
        if self.held_target:
            key = "meta:telemetry-day:2024-12-20"
            source_fence = {**source_fence, key: self.markers.get(key, "")}
        if self.negative_input:
            data = replace(data, negative_input_guards=((self.negative_input, int(origin.timestamp())),))
        return data, {"version": "synthetic-selection"}, source_fence


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> CacheWorld:
    value = CacheWorld()

    class Store:
        def __init__(self, db: object) -> None:
            self.repository = value
            self.read = value.read
            self.save = value.save

    monkeypatch.setattr(reports, "RegulationFactStore", Store)
    monkeypatch.setattr(reports, "regulation_scopes", lambda devices, series: [("device", "circuit")])
    monkeypatch.setattr(reports, "regulation_source_selections", lambda devices, series: {
        "device/circuit": {"version": "synthetic-selection"}})
    monkeypatch.setattr(reports, "prepare_regulation_day", value.prepare)
    return value


def read(world: CacheWorld, day: int, *, calculate: bool = True) -> dict:
    start = START + timedelta(days=day)
    return reports.validated_regulation_days(world, start, start + timedelta(days=1), timezone="UTC",
                                             calculate_daily=calculate)


def test_repeat_uses_compact_facts_and_future_configuration_change_does_not_replay_history(world: CacheWorld) -> None:
    first = read(world, 2)
    assert first["built_days"] == 2 and len(world.raw_reads) == 2
    repeated = read(world, 2)
    assert repeated["built_days"] == 0 and repeated["hit_days"] == 2
    assert len(world.raw_reads) == 2
    world.metadata_revision += 1
    world.future_change = True
    later = read(world, 2)
    assert later["built_days"] == 0 and later["hit_days"] == 2
    assert len(world.raw_reads) == 2
    assert later["circuits"][0]["facts"][0]["metrics"] == first["circuits"][0]["facts"][0]["metrics"]


def test_late_predecessor_rebuild_is_bounded_and_unrelated_old_cache_remains_usable(world: CacheWorld) -> None:
    read(world, 2)
    read(world, 6)
    world.markers["meta:telemetry-day:2025-01-02"] = "late-revision"
    before = len(world.raw_reads)
    changed = read(world, 2)
    assert changed["built_days"] == 2 and len(world.raw_reads) - before == 2
    unaffected = read(world, 6)
    assert unaffected["built_days"] == 0 and unaffected["hit_days"] == 2


def test_late_week_old_unresolved_recovery_is_not_silently_forgotten_or_replayed(world: CacheWorld) -> None:
    world.unresolved_dhw = True
    read(world, 2)
    read(world, 3)
    before = len(world.raw_reads)
    world.markers["meta:telemetry-day:2025-01-01"] = "late-ancestor"
    result = read(world, 4)
    assert len(world.raw_reads) == before
    assert result["circuits"][0]["facts"] == []
    assert result["circuits"][0]["missing_days"][0]["reason"] == "stale_boundary_ancestor_requires_local_backfill"


def test_season_without_facts_is_explicitly_missing_and_never_scans_raws(world: CacheWorld) -> None:
    result = reports.validated_regulation_days(world, START, START + timedelta(days=60), timezone="UTC")
    assert not world.raw_reads
    assert len(result["circuits"][0]["missing_days"]) == 60
    assert result["circuits"][0]["facts"] == []


def test_late_setpoint_inside_old_hold_invalidates_current_day_but_not_by_global_revision(world: CacheWorld) -> None:
    world.held_target = True
    read(world, 6)
    assert read(world, 6)["built_days"] == 0
    world.markers["meta:telemetry-day:2024-12-20"] = "late-held-setpoint"
    revised = read(world, 6)
    assert revised["built_days"] == 2


@pytest.mark.parametrize("key", ["regulation-first-setpoint:42", "regulation-first-event:device:controller"])
def test_negative_historical_input_survives_future_first_arrival_but_rejects_late_old_first_arrival(
    world: CacheWorld, key: str,
) -> None:
    world.negative_input = key
    read(world, 6)
    assert read(world, 6, calculate=False)["hit_days"] == 2
    world.markers["meta:" + key] = str(int((START + timedelta(days=100)).timestamp()))
    assert read(world, 6, calculate=False)["hit_days"] == 2
    world.markers["meta:" + key] = str(int((START - timedelta(days=10)).timestamp()))
    assert read(world, 6, calculate=False)["circuits"][0]["facts"] == []


def test_offline_export_cannot_drop_rejected_unresolved_ancestor(
    world: CacheWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    from tools import backfill_regulation

    world.unresolved_dhw = True
    read(world, 2)
    read(world, 3)
    world.markers["meta:telemetry-day:2025-01-01"] = "late-ancestor"
    monkeypatch.setattr(backfill_regulation, "RegulationRepository", lambda storage: world)
    with pytest.raises(ValueError, match="earlier bounded export"):
        backfill_regulation.export_selected_period(world, START + timedelta(days=4), START + timedelta(days=5),
                                                   timezone="UTC", output=tmp_path / "private.json")
