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
    assert incremental._dependents(items, changed, START) == {item["href"]}
    assert item["dirty"] & incremental.RENDER
