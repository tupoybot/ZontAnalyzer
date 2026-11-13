from __future__ import annotations

import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import ModuleType, SimpleNamespace

import pytest

from zont_analyzer.application import incremental_publication as incremental
from zont_analyzer.application import regulation_refresh as refresh
from zont_analyzer.application.reasoning_context import report_facts_fingerprint, reuse_ai_interpretation
from zont_analyzer.domain import QualityResult, Report

START = datetime(2026, 1, 5, tzinfo=UTC)


def context(*, error: float = 0.2, missing: bool = False) -> dict:
    return {
        "regulation_facts": {
            "incomplete": missing, "cache": {"built_days": 1, "hit_days": 0},
            "dependency_start": (START - timedelta(days=40)).isoformat(),
            "dependency_end": (START + timedelta(days=1)).isoformat(),
            "circuits": [] if missing else [{"scope": "device/1", "metrics": {
                "room_absolute_error_c": {"value": error}, "cache": {"value": 7},
            }}],
        },
        "regulation_comparisons": {
            "history_start": (START - timedelta(days=28)).isoformat(),
            "history_end": (START + timedelta(days=1)).isoformat(),
            "scope": "device/1", "results": [] if missing else [{
                "stage": "available", "dependency_fingerprint": "source-a",
                "source_facts": [{"fingerprint": "a", "start": START.isoformat()}],
                "metrics": {"room_absolute_error_c": {"after": error}},
            }],
        },
    }


def report(*, ai: bool = True) -> Report:
    return Report(
        id="regulation-refresh", kind="daily", period_start=START, period_end=START + timedelta(days=1),
        generated_at=START + timedelta(days=1), timezone="UTC", summary="Original interpretation", ai_used=ai,
        context=context(), quality=QualityResult(score=1, coverage_pct=100, max_gap_seconds=0,
                                                stuck_pct=0, implausible_jumps=0, sample_count=0),
    )


def test_refresh_uses_shared_validated_compact_reader_without_daily_calculation(monkeypatch) -> None:
    original = report()
    baseline = report_facts_fingerprint(original)
    original.context["ai_facts_fingerprint"] = baseline
    calls = []
    module = ModuleType("zont_analyzer.application.regulation_comparison_reports")

    def build(db, start, end, **kwargs):
        calls.append((db, start, end, kwargs))
        return context(error=0.8)

    module.build_regulation_report_context = build
    monkeypatch.setitem(sys.modules, module.__name__, module)
    database = object()
    result = refresh.refresh_regulation_context(database, original)
    assert calls == [(database, original.period_start, original.period_end,
                      {"timezone": "UTC", "calculate_daily": False})]
    assert result.summary == original.summary and result.ai_used
    assert result.recommendations == original.recommendations
    assert result.context["ai_facts_fingerprint"] == baseline
    assert result.context["regulation_interpretation_stale"] is True
    assert original.context["regulation_facts"]["circuits"][0]["metrics"]["room_absolute_error_c"]["value"] == 0.2


def test_typed_semantics_ignore_bookkeeping_but_retain_nested_evidence(monkeypatch) -> None:
    original = report()
    updated = deepcopy(original.context)
    updated["regulation_facts"]["cache"] = {"built_days": 0, "hit_days": 1}
    updated["regulation_comparisons"]["results"][0]["dependency_fingerprint"] = "source-b"
    updated["regulation_comparisons"]["results"][0]["source_facts"][0]["fingerprint"] = "b"
    monkeypatch.setattr(refresh, "_build_context", lambda *_: updated)
    assert refresh.refresh_regulation_context(object(), original) is original
    equivalent = original.model_copy(update={"context": updated})
    equivalent.context["regulation_interpretation_stale"] = True
    assert report_facts_fingerprint(equivalent) == report_facts_fingerprint(original)
    updated["regulation_facts"]["circuits"][0]["metrics"]["cache"]["value"] = 8
    assert refresh.regulation_context_fingerprint(updated) != refresh.regulation_context_fingerprint(original.context)


@pytest.mark.parametrize("prior", ["pilot_ai_reuse", "regulation_interpretation_stale",
                                  "control_settings_interpretation_stale", "gas_interpretation_stale"])
def test_unknown_original_ai_evidence_stays_unknown_across_refreshes(monkeypatch, prior: str) -> None:
    original = report()
    original.context[prior] = {"facts_changed": True} if prior == "pilot_ai_reuse" else True
    for error in (0.4, 0.8):
        monkeypatch.setattr(refresh, "_build_context", lambda *_, error=error: context(error=error))
        original = refresh.refresh_regulation_context(object(), original)
        assert "ai_facts_fingerprint" not in original.context
        assert original.context["regulation_interpretation_stale"] is True


def test_stale_cache_is_replaced_with_missing_and_never_keeps_old_comparison(monkeypatch) -> None:
    original = report(ai=False)
    monkeypatch.setattr(refresh, "_build_context", lambda *_: context(missing=True))
    result = refresh.refresh_regulation_context(object(), original)
    assert result.context["regulation_facts"]["incomplete"]
    assert result.context["regulation_facts"]["circuits"] == []
    assert result.context["regulation_comparisons"]["results"] == []
    assert "regulation_interpretation_stale" not in result.context
    monkeypatch.setattr(refresh, "_build_context", lambda *_: {"regulation_facts": {}})
    with pytest.raises(ValueError, match="facts and comparisons"):
        refresh.refresh_regulation_context(object(), original)


def test_ai_reuse_preserves_regulation_staleness_and_original_baseline() -> None:
    original = report()
    baseline = report_facts_fingerprint(original)
    original.context["ai_facts_fingerprint"] = baseline
    current = original.model_copy(update={"context": context(error=0.8)})
    reused = reuse_ai_interpretation(original, current)
    assert reused.context["regulation_interpretation_stale"]
    assert reused.context["ai_facts_fingerprint"] == baseline
    assert reused.summary == original.summary


@pytest.mark.parametrize("scope", ["regulation-facts", "regulation-manual"])
def test_late_day_queues_existing_dependent_scope_without_gas_or_archive_scan(scope: str) -> None:
    selected = report()
    foreign = selected.model_copy(deep=True)
    foreign.id = "foreign"
    foreign.context["regulation_facts"]["circuits"][0]["scope"] = "other/1"
    foreign.context["regulation_comparisons"]["scope"] = "other/1"
    nearby = selected.model_copy(deep=True)
    nearby.id = "nearby"
    nearby.context = {}
    reports = {value.id: value for value in (selected, foreign, nearby)}
    items = {}
    for value in reports.values():
        lo, hi = incremental._windows(value)
        items[value.id] = {"report_id": value.id, "href": value.id, "lo": lo, "hi": hi, "dirty": 0}
    reads = []

    def read(identifier):
        reads.append(identifier)
        return reports[identifier]

    boundary = START - timedelta(days=35)
    incremental._regulation_change(items, {
        "scope": scope, "identifier": "device/1", "payload": {
            "change_start": int(boundary.timestamp() * 1_000_000),
            "change_end": int((boundary + timedelta(days=1)).timestamp() * 1_000_000),
        },
    }, SimpleNamespace(report=read), START)
    assert reads == [selected.id, foreign.id]
    assert items[selected.id]["dirty"] == incremental.RENDER
    assert items[foreign.id]["dirty"] == items[nearby.id]["dirty"] == 0
    assert refresh.report_depends_on_regulation_scope(selected, "device/*")


def test_new_daily_facts_requeue_later_regulation_report_without_legacy_comparisons(tmp_path) -> None:
    selected = report(ai=False)
    items = {}
    assert incremental._remember(items, tmp_path, selected, START + timedelta(days=2), cloud=True)
    item, = items.values()
    item["dirty"] = 0
    changed_start = START - timedelta(days=35)
    changed = selected.model_copy(update={
        "id": "changed-earlier-day", "period_start": changed_start,
        "period_end": changed_start + timedelta(days=1),
    })
    database = SimpleNamespace(report=lambda _: selected)
    assert incremental._dependents(items, changed, START, db=database) == {item["href"]}
    assert item["dirty"] == incremental.RENDER


def test_unresolved_regulation_envelopes_do_not_broaden_legacy_dependencies():
    selected = report(ai=False)
    selected.context = {"regulation_facts": {"circuits": [], "incomplete": True,
                                               "dependency_start": "1970-01-01T00:00:00+00:00",
                                               "dependency_end": selected.period_end.isoformat()},
                        "regulation_comparisons": {"scopes": [], "results": [],
                                                   "history_start": (START - timedelta(days=28)).isoformat(),
                                                   "history_end": selected.period_end.isoformat()}}
    assert incremental._windows(selected) == incremental._gas_windows(selected)
    assert not incremental._has_dependencies(selected)


def test_regulation_baseline_telemetry_queues_render_without_gas_refresh():
    selected = report(ai=False)
    lo, hi = incremental._windows(selected)
    item = {"report_id": selected.id, "href": selected.id, "lo": lo, "hi": hi, "dirty": 0}
    incremental._telemetry({selected.id: item}, [{"identifier": "2026-01-02T00"}], START,
                           broad_calibration=False, db=SimpleNamespace(report=lambda _: selected))
    assert item["dirty"] == incremental.RENDER


@pytest.mark.ydb
def test_changed_daily_report_and_late_telemetry_refresh_regulation_only_dependents(tmp_path, monkeypatch):
    from tests.unit.test_incremental_publication import _daily_reports, _record_change, _runtime
    from zont_analyzer.application import publication
    from zont_analyzer.application.gas import GasService

    runtime = _runtime(tmp_path)
    source, unrelated, dependent = _daily_reports(runtime, 3)
    dependent.context.update(context())
    dependent.context["regulation_facts"].update(
        dependency_start=source.period_start.isoformat(), dependency_end=dependent.period_end.isoformat(),
    )
    dependent.context["regulation_comparisons"].update(
        history_start=source.period_start.isoformat(), history_end=dependent.period_end.isoformat(),
    )
    runtime.db.save_report(dependent, "fixture")
    revision = [0]

    def compact_only(_db, value):
        current = {key: deepcopy(value.context[key]) for key in refresh.REGULATION_KEYS}
        if value.id == dependent.id:
            current["regulation_facts"]["circuits"][0]["metrics"]["room_absolute_error_c"]["value"] = revision[0]
        return current

    monkeypatch.setattr(refresh, "_build_context", compact_only)
    publication.publish_reports(runtime, batch_size=8)
    saved_unrelated = runtime.db.report(unrelated.id)
    original_refresh = GasService.refresh
    gas_calls = []

    def gas_only_own_period(service, value):
        gas_calls.append(value.id)
        assert value.id != dependent.id, "a regulation baseline must not trigger gas recomputation"
        return original_refresh(service, value)

    monkeypatch.setattr(GasService, "refresh", gas_only_own_period)
    revision[0] = 1
    changed = runtime.db.report(source.id)
    assert changed is not None
    changed.summary = "Updated daily source"
    runtime.db.save_report(changed, changed.summary)
    result = publication.publish_reports(runtime, batch_size=8)
    assert result["rendered_reports"] == 2 and result["pending_reports"] == 0
    assert gas_calls == [source.id]
    assert runtime.db.report(unrelated.id) == saved_unrelated
    assert runtime.db.report(dependent.id).context["regulation_facts"]["circuits"][0]["metrics"][
        "room_absolute_error_c"]["value"] == 1
    gas_calls.clear()
    revision[0] = 2
    hour = (source.period_start + timedelta(hours=12)).strftime("%Y-%m-%dT%H")
    _record_change(runtime.db, "telemetry", hour, {"calibration": False})
    result = publication.publish_reports(runtime, batch_size=8)
    assert result["rendered_reports"] == 2 and result["pending_reports"] == 0
    assert gas_calls == [source.id]
    assert runtime.db.report(unrelated.id) == saved_unrelated
    assert publication.publish_reports(runtime)["rendered_reports"] == 0


@pytest.mark.ydb
@pytest.mark.parametrize("additional_refresh", [None, "cost", "gas"])
def test_settings_backlog_continues_across_batches_with_existing_flag_combinations(
    tmp_path, monkeypatch, additional_refresh,
):
    from tests.unit.test_incremental_publication import _daily_reports, _record_change, _runtime
    from zont_analyzer.application import publication

    runtime = _runtime(tmp_path)
    reports = _daily_reports(runtime, 5)
    for value in reports:
        value.context["control_settings_history"] = {"device_id": "fixture", "circuit_id": "1", "versions": []}
        runtime.db.save_report(value, value.summary)
    publication.publish_reports(runtime, batch_size=8)
    calls = []

    def history(device, circuit, start, end):
        calls.append((device, circuit, start))
        return {"device_id": device, "circuit_id": circuit, "versions": [], "fingerprint": "updated"}

    monkeypatch.setattr(runtime.db, "control_settings_history", history)
    if additional_refresh:
        _record_change(runtime.db, "global", additional_refresh)
    _record_change(runtime.db, "control-settings", "fixture", {
        "change_start": int(reports[0].period_start.timestamp() * 1_000_000),
        "change_end": int(reports[-1].period_end.timestamp() * 1_000_000),
    })
    rendered = 0
    for pending in (3, 1, 0):
        result = publication.publish_reports(runtime, batch_size=2)
        rendered += result["rendered_reports"]
        assert result["pending_reports"] == pending
    assert rendered == 5
    assert {start for _device, _circuit, start in calls} == {value.period_start for value in reports}
    assert publication.publish_reports(runtime, batch_size=2)["rendered_reports"] == 0
