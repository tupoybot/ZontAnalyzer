"""The combined report reader shares validated full history, never raw lookback."""
from __future__ import annotations

import sys
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from zont_analyzer.application import regulation_comparison_reports as combined
from zont_analyzer.application.regulation_reports import day_history
from zont_analyzer.domain.periods import midnight

START = datetime(2026, 1, 10, tzinfo=UTC)
END = START + timedelta(days=1)


def local_days(start: datetime, end: datetime, timezone: str) -> list[tuple[datetime, datetime]]:
    day = start.astimezone(ZoneInfo(timezone)).date()
    result = []
    while midnight(day, timezone) < end:
        result.append((midnight(day, timezone), midnight(day + timedelta(days=1), timezone)))
        if len(result) > 400:
            raise ValueError("regulation report is limited to 400 local days")
        day += timedelta(days=1)
    return result


def fact(start: datetime, end: datetime, *, circuit: str = "left") -> dict[str, Any]:
    return {"scope": f"device/{circuit}", "period_start": start.isoformat(), "period_end": end.isoformat(),
            "runs": [{"provenance": circuit}]}


def circuit(facts: list[dict[str, Any]], *, name: str = "left",
            missing: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"scope": f"device/{name}", "device_id": "device", "circuit_id": name,
            "facts": facts, "missing_days": missing or [], "expected_days": 29,
            "settings_history": {"device_id": "device", "circuit_id": name, "versions": [], "incomplete": False},
            "settings_history_incomplete": False, "source_binding": f"source-{name}"}


def manual(identifier: str, *, name: str | None = None, device: str | None = "device",
           undated: bool = False) -> dict[str, Any]:
    return {"intervention_id": identifier, "temporal_boundary": None if undated else START.isoformat(),
            "experiment": {"changes": [{"device_id": device, "circuit_id": name, "parameter": "pid_prop_koef"}],
                           "performed_at": None if undated else START.isoformat()}}


class DatabaseStub:
    def __init__(self) -> None:
        self.storage = object()
        self.dated: list[dict[str, Any]] = []
        self.undated: list[dict[str, Any]] = []
        self.manual_reads: list[tuple[str, int, datetime | None]] = []

    def intervention_history(self, limit: int, *, before: datetime) -> list[dict[str, Any]]:
        self.manual_reads.append(("dated", limit, before))
        return self.dated


class ReaderHarness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.db = DatabaseStub()
        self.selection: dict[str, Any] = {"circuits": [circuit([fact(START, END)])],
                                         "built_days": 0, "hit_days": 30, "unknowns": [],
                                         "dependency_start": (START - timedelta(days=30)).isoformat(),
                                         "dependency_end": END.isoformat()}
        self.validations: list[dict[str, Any]] = []
        self.aggregations: list[dict[str, Any]] = []
        self.comparisons: list[dict[str, Any]] = []
        dependency = ModuleType("zont_analyzer.application.regulation_reports")
        monkeypatch.setattr(dependency, "_days", local_days, raising=False)
        monkeypatch.setattr(dependency, "day_history", day_history, raising=False)
        monkeypatch.setattr(dependency, "validated_regulation_days", self.validate, raising=False)
        monkeypatch.setattr(dependency, "build_regulation_context", self.aggregate, raising=False)
        monkeypatch.setitem(sys.modules, dependency.__name__, dependency)
        monkeypatch.setattr(combined, "build_regulation_comparison_context", self.compare)
        harness = self

        class OwnerRepositoryStub:
            def __init__(self, storage: object) -> None:
                assert storage is harness.db.storage

            def uncertain_history(self, limit: int) -> list[dict[str, Any]]:
                harness.db.manual_reads.append(("undated", limit, None))
                return harness.db.undated

        monkeypatch.setattr(combined, "OwnerExperimentsRepository", OwnerRepositoryStub)

    def validate(self, db: object, start: datetime, end: datetime, *, timezone: str,
                 calculate_daily: bool) -> dict[str, Any]:
        assert db is self.db
        self.validations.append({"start": start, "end": end, "timezone": timezone,
                                 "calculate_daily": calculate_daily})
        if calculate_daily:
            return {**self.selection, "built_days": 2}
        return self.selection

    def aggregate(self, db: object, start: datetime, end: datetime, *, timezone: str,
                  calculate_daily: bool, selected: dict[str, Any]) -> dict[str, Any]:
        assert db is self.db
        assert not calculate_daily
        self.aggregations.append(selected)
        return {"regulation_facts": {"circuits": selected["circuits"], "unknowns": selected["unknowns"]}}

    def compare(self, db: object, *, device_id: str, circuit_id: str, start: datetime, end: datetime,
                read_facts: Any, timeline: dict[str, Any], manual_history: list[dict[str, Any]]) -> dict[str, Any]:
        assert db is self.db
        scope = f"{device_id}/{circuit_id}"
        left = min(start, end - timedelta(days=28)).replace(hour=0, minute=0, second=0, microsecond=0)
        data = read_facts(scope, left, end)
        assert all(item["scope"] == scope for item in data)
        self.comparisons.append({"scope": scope, "facts": data, "manual": manual_history, "timeline": timeline})
        return {"regulation_comparisons": {
            "algorithm_version": "regulation-comparison-v2", "scope": scope,
            "results": [{"event_id": scope, "event": {"boundary": {"no_later_than": START.isoformat()}},
                         "stage": "available", "outcome": "mixed", "next_observation": []}],
            "total_changes_in_history": 1, "events_truncated": False, "history_incomplete": False,
            "manual_history_incomplete": False, "unmatched_manual_count": len(manual_history),
            "fact_days_read": len(data),
        }}

    def build(self, *, start: datetime = START, end: datetime = END,
              timezone: str = "UTC", calculate_daily: bool = False) -> dict[str, Any]:
        return combined.build_regulation_report_context(
            self.db, start, end, timezone=timezone, calculate_daily=calculate_daily)  # type: ignore[arg-type]


@pytest.fixture
def reader(monkeypatch: pytest.MonkeyPatch) -> ReaderHarness:
    return ReaderHarness(monkeypatch)


def test_period_and_comparison_share_one_validated_compact_range(reader: ReaderHarness) -> None:
    old = fact(START - timedelta(days=20), START - timedelta(days=19))
    reader.selection["circuits"][0]["facts"].insert(0, old)
    original = deepcopy(reader.selection)
    context = reader.build()
    assert len(reader.validations) == 1
    assert not reader.validations[0]["calculate_daily"]
    assert len(reader.comparisons[0]["facts"]) == 2
    selected = reader.aggregations[0]
    assert selected["circuits"][0]["facts"] == [fact(START, END)]
    assert selected["hit_days"] == 1
    assert selected["circuits"][0]["expected_days"] == 1
    assert selected["dependency_start"] == reader.selection["dependency_start"]
    assert context["regulation_comparisons"]["fact_days_read"] == 2
    assert reader.selection == original


def test_only_explicit_daily_calculation_can_build_raw_days(reader: ReaderHarness) -> None:
    reader.build(calculate_daily=True)
    assert [call["calculate_daily"] for call in reader.validations] == [True, False]
    assert reader.validations[0]["start"] == START
    assert reader.validations[0]["end"] == END
    assert reader.aggregations[0]["built_days"] == 2


def test_long_period_never_turns_calculate_daily_into_raw_backfill(reader: ReaderHarness) -> None:
    reader.build(start=START - timedelta(days=90), calculate_daily=True)
    assert len(reader.validations) == 1
    assert not reader.validations[0]["calculate_daily"]
    assert reader.validations[0]["start"] == START - timedelta(days=90)


@pytest.mark.parametrize("timezone", ["Europe/Samara", "America/New_York"])
def test_local_first_day_covers_utc_lookback_and_is_shared(reader: ReaderHarness, timezone: str) -> None:
    start, end = midnight(date(2026, 3, 1), timezone), midnight(date(2026, 3, 2), timezone)
    history = min(start, end - timedelta(days=28)).replace(hour=0, minute=0, second=0, microsecond=0)
    first_day = midnight(history.astimezone(ZoneInfo(timezone)).date(), timezone)
    covering = fact(first_day, midnight(first_day.astimezone(ZoneInfo(timezone)).date() + timedelta(days=1), timezone))
    reader.selection["circuits"][0]["facts"] = [covering, fact(start, end)]
    context = reader.build(start=start, end=end, timezone=timezone)
    assert first_day <= history < datetime.fromisoformat(covering["period_end"])
    assert reader.validations[0]["start"] == first_day
    assert covering in reader.comparisons[0]["facts"]
    assert reader.aggregations[0]["circuits"][0]["facts"] == [fact(start, end)]
    assert context["regulation_comparisons"]["validated_history_start"] == first_day.isoformat()


def test_dst_day_is_one_local_day_with_23_observed_hours(reader: ReaderHarness) -> None:
    start, end = midnight(date(2026, 3, 29), "Europe/Berlin"), midnight(date(2026, 3, 30), "Europe/Berlin")
    assert (end - start).total_seconds() == 23 * 3600
    reader.selection["circuits"][0]["facts"] = [fact(start, end)]
    reader.build(start=start, end=end, timezone="Europe/Berlin", calculate_daily=True)
    assert [call["calculate_daily"] for call in reader.validations] == [True, False]
    assert reader.aggregations[0]["circuits"][0]["expected_days"] == 1
    assert reader.aggregations[0]["circuits"][0]["missing_days"] == []


def test_partial_report_days_are_counted_and_never_replaced_by_full_day_facts(reader: ReaderHarness) -> None:
    start, end = START + timedelta(hours=6), START + timedelta(days=2, hours=18)
    reader.selection["circuits"][0] = circuit(
        [fact(START, END), fact(END, END + timedelta(days=1))],
        missing=[{"start": (START + timedelta(days=2)).isoformat(),
                  "end": (START + timedelta(days=3)).isoformat(),
                  "reason": "stale_boundary_ancestor_requires_backfill"}],
    )
    context = reader.build(start=start, end=end)
    selected = reader.aggregations[0]["circuits"][0]
    assert selected["expected_days"] == 3
    assert selected["facts"] == [fact(END, END + timedelta(days=1))]
    assert len(selected["missing_days"]) == 2
    assert all(day["reason"] == "partial_local_day" for day in selected["missing_days"])
    missing = context["regulation_comparisons"]["history_missing_days"]
    assert missing[0]["scope"] == "device/left"
    assert missing[0]["reason"] == "stale_boundary_ancestor_requires_backfill"
    assert context["regulation_comparisons"]["history_incomplete"]


def test_scoped_and_unknown_manual_actions_are_preserved_even_without_facts(reader: ReaderHarness) -> None:
    reader.selection["circuits"] = [circuit([], name="left"),
                                    circuit([fact(START, END, circuit="right")], name="right")]
    reader.db.undated = [manual("undated-left", name="left", undated=True)]
    reader.db.dated = [manual("right", name="right"), manual("unknown", device=None), manual("device-wide")]
    context = reader.build()
    left, right = reader.comparisons
    assert [item["intervention_id"] for item in left["manual"]] == ["undated-left", "unknown", "device-wide"]
    assert [item["intervention_id"] for item in right["manual"]] == ["right", "unknown", "device-wide"]
    assert left["manual"][0]["experiment"]["performed_at"] is None
    assert context["regulation_comparisons"]["scopes"] == ["device/left", "device/right"]
    assert context["regulation_comparisons"]["manual_source_counts"] == {"undated": 1, "dated": 3}
    assert all(result["scope"] == result["event_id"] for result in context["regulation_comparisons"]["results"])


def test_scope_filter_cannot_hide_global_manual_read_overflow(reader: ReaderHarness) -> None:
    reader.db.undated = [manual(str(index), name="other", undated=True) for index in range(101)]
    context = reader.build()["regulation_comparisons"]
    assert reader.comparisons[0]["manual"] == []
    assert context["manual_history_incomplete"]
    assert context["results"][0]["stage"] == "insufficient"
    assert context["results"][0]["outcome"] is None
    assert "manual_history_incomplete; source_read_limit_reached" in context["results"][0]["next_observation"]


def test_unresolved_source_scope_keeps_unknowns_and_skips_manual_reads(reader: ReaderHarness) -> None:
    reader.selection.update(circuits=[], unknowns=["heating_scope_unresolved"])
    context = reader.build()
    assert reader.db.manual_reads == []
    assert reader.comparisons == []
    assert context["regulation_comparisons"]["history_incomplete"]
    assert context["regulation_comparisons"]["unknowns"] == ["heating_scope_unresolved"]
    assert context["regulation_facts"]["unknowns"] == ["heating_scope_unresolved"]


@pytest.mark.parametrize(("start", "end"), [
    (START.replace(tzinfo=None), END), (START, START), (END, START),
    (START, START + timedelta(days=401)),
])
def test_invalid_or_oversized_period_does_no_calculation(reader: ReaderHarness, start: datetime, end: datetime) -> None:
    with pytest.raises(ValueError):
        reader.build(start=start, end=end, calculate_daily=True)
    assert reader.validations == []


def test_real_comparison_builder_consumes_shared_validated_facts(reader: ReaderHarness,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    from zont_analyzer.application.regulation_comparison import build_regulation_comparison_context

    monkeypatch.setattr(combined, "build_regulation_comparison_context", build_regulation_comparison_context)
    values: dict[str, str] = {}
    monkeypatch.setattr(reader.db, "get_app_meta", values.get, raising=False)
    monkeypatch.setattr(reader.db, "set_app_meta", values.__setitem__, raising=False)
    history_start = END - timedelta(days=28)
    old = {"status": "observed", "fields": {"pid_prop_koef": {"status": "observed", "value": 1}}}
    new = {"status": "observed", "fields": {"pid_prop_koef": {"status": "observed", "value": 2}}}
    history = {"device_id": "device", "circuit_id": "left", "incomplete": False, "versions": [
        {"event_id": "first", "kind": "first_observation", "observed_at": int(history_start.timestamp() * 1_000_000),
         "version": "old", "state": old},
        {"event_id": "change", "kind": "change", "observed_at": int(START.timestamp() * 1_000_000),
         "version": "new", "state": new, "changed_fields": ["pid_prop_koef"],
         "boundary": {"after": int((START - timedelta(hours=1)).timestamp() * 1_000_000),
                      "no_later_than": int(START.timestamp() * 1_000_000)}},
    ]}
    monkeypatch.setattr(reader.db, "control_settings_history", lambda *args: history, raising=False)
    reader.selection["circuits"][0]["settings_history"] = history
    source = {**fact(START, END), "algorithm_version": "regulation-v1", "source_fingerprint": "validated-source",
              "settings_version": "new", "request_semantics": "state", "runs": []}
    reader.selection["circuits"][0]["facts"] = [source]
    reader.db.undated = [manual("unknown-time", name="left", undated=True)]
    result = reader.build()["regulation_comparisons"]
    assert len(reader.validations) == 1
    assert result["results"][0]["source_facts"][0]["fingerprint"] == "validated-source"
    assert result["results"][0]["unplaced_manual_confounds"] == ["unknown-time"]
    assert result["results"][0]["scope"] == "device/left"
    assert result["results"][0]["stage"] == "insufficient"
    assert result["fact_days_read"] == 1
    assert len(values) == 1


def test_comparison_reuses_already_validated_settings_history_without_another_read(
    reader: ReaderHarness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zont_analyzer.application.regulation_comparison import build_regulation_comparison_context

    monkeypatch.setattr(combined, "build_regulation_comparison_context", build_regulation_comparison_context)
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("settings history must be reused from the validated selection")
    monkeypatch.setattr(reader.db, "control_settings_history", forbidden, raising=False)
    result = reader.build()["regulation_comparisons"]
    assert len(reader.validations) == 1
    assert result["results"] == []
    assert result["scopes"] == ["device/left"]
