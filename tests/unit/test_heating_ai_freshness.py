from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from zont_analyzer.application import regulation_refresh
from zont_analyzer.application.reasoning_context import (
    _facts_digest,
    reconcile_ai_evidence,
    report_facts_fingerprint,
    reuse_ai_interpretation,
)
from zont_analyzer.application.settings_history import refresh_settings_history
from zont_analyzer.domain import QualityResult, Report


def _report() -> Report:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    report = Report(
        id="freshness-roundtrip", kind="daily", period_start=start, period_end=start + timedelta(days=1),
        generated_at=start + timedelta(days=1), ai_used=True, summary="Saved analysis of the original facts",
        quality=QualityResult(score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
                              implausible_jumps=0, sample_count=100),
        context={
            "regulation_facts": {"circuits": [{"metrics": {"room_absolute_error_c": {"value": 0.2}}}]},
            "regulation_comparisons": {"results": []},
            "control_settings_history": {"device_id": "fixture", "circuit_id": "1", "versions": []},
        },
    )
    report.context["ai_facts_fingerprint"] = report_facts_fingerprint(report)
    return report


@pytest.mark.parametrize("component", ["regulation", "settings"])
def test_original_evidence_recovery_clears_stale_flag_without_replacing_ai(monkeypatch, component: str) -> None:
    original = _report()
    saved = original.model_dump_json()
    baseline = original.context["ai_facts_fingerprint"]
    if component == "regulation":
        changed = deepcopy(original.context)
        changed["regulation_facts"] = {"circuits": [], "incomplete": True}
        current = [changed]
        monkeypatch.setattr(regulation_refresh, "_build_context", lambda *_: current[0])
        def refresh(report):
            return regulation_refresh.refresh_regulation_context(object(), report)
        restored = original.context
        flag = "regulation_interpretation_stale"
    else:
        current = [{**original.context["control_settings_history"], "versions": [{"value": 3}]}]
        db = SimpleNamespace(control_settings_history=lambda *_: current[0])
        def refresh(report):
            return refresh_settings_history(db, report)
        restored = original.context["control_settings_history"]
        flag = "control_settings_interpretation_stale"
    stale = refresh(original)
    assert stale.context[flag]
    assert stale.context["ai_facts_fingerprint"] == baseline
    current[0] = restored
    repaired = refresh(stale)
    assert flag not in repaired.context
    assert repaired.summary == original.summary
    assert repaired.context["ai_facts_fingerprint"] == baseline
    assert refresh(repaired) is repaired
    assert original.model_dump_json() == saved


def test_returning_to_original_facts_does_not_inherit_stale_flags_during_ai_reuse() -> None:
    original = _report()
    changed = original.model_copy(deep=True)
    changed.context["regulation_facts"]["circuits"][0]["metrics"]["room_absolute_error_c"]["value"] = 0.8
    stale = reuse_ai_interpretation(original, changed)
    assert stale.context["regulation_interpretation_stale"]
    repaired = reuse_ai_interpretation(stale, original)
    assert not repaired.context.get("regulation_interpretation_stale")
    assert repaired.context["pilot_ai_reuse"]["facts_changed"] is False


@pytest.mark.parametrize("baseline", [None, "unknown-old-digest", "facts-v2:unmatched"])
def test_missing_original_evidence_is_never_replaced_by_current_facts(baseline: str | None) -> None:
    report = _report()
    report.context["regulation_interpretation_stale"] = True
    if baseline is None:
        report.context.pop("ai_facts_fingerprint")
    else:
        report.context["ai_facts_fingerprint"] = baseline
    saved = report.model_dump_json()
    assert reconcile_ai_evidence(report) is report
    assert report.model_dump_json() == saved


def test_matching_evidence_can_repair_a_sticky_flag_without_another_refresh() -> None:
    report = _report()
    report.context["regulation_interpretation_stale"] = True
    repaired = reconcile_ai_evidence(report)
    assert "regulation_interpretation_stale" not in repaired.context
    assert report.context["regulation_interpretation_stale"] is True


def _bounded_report(*, legacy: bool = False) -> Report:
    report = _report()
    report.context["regulation_facts"].update(
        dependency_start="2025-12-31T00:00:00+00:00", dependency_end="2026-01-03T00:00:00+00:00",
    )
    report.context["ai_facts_fingerprint"] = (
        _facts_digest(report, include_ai_provenance=True) if legacy else report_facts_fingerprint(report)
    )
    return report


@pytest.mark.parametrize("legacy", [False, True])
def test_dependency_bounds_migrate_only_after_exact_original_evidence_proof(monkeypatch, legacy: bool) -> None:
    original = _bounded_report(legacy=legacy)
    original.context["regulation_interpretation_stale"] = True
    saved = original.model_dump_json()
    current = deepcopy(original.context)
    current["regulation_facts"].update(
        dependency_start="2025-12-31T18:00:00+00:00", dependency_end="2026-01-02T21:00:00+00:00",
    )
    saved_current = deepcopy(current)
    monkeypatch.setattr(regulation_refresh, "_build_context", lambda *_: current)
    repaired = regulation_refresh.refresh_regulation_context(object(), original)
    assert repaired.context["ai_facts_fingerprint"] == report_facts_fingerprint(repaired)
    assert repaired.context["ai_facts_fingerprint"] != original.context["ai_facts_fingerprint"]
    assert "regulation_interpretation_stale" not in repaired.context
    assert repaired.context["regulation_facts"] == current["regulation_facts"]
    assert repaired.summary == original.summary and repaired.recommendations == original.recommendations
    assert original.model_dump_json() == saved and current == saved_current
    assert regulation_refresh.refresh_regulation_context(object(), repaired) is repaired


@pytest.mark.parametrize("legacy", [False, True])
def test_original_metric_recovery_can_migrate_changed_dependency_bounds(monkeypatch, legacy: bool) -> None:
    original = _bounded_report(legacy=legacy)
    changed = deepcopy(original.context)
    changed["regulation_facts"]["circuits"][0]["metrics"]["room_absolute_error_c"]["value"] = 0.8
    current = [changed]
    monkeypatch.setattr(regulation_refresh, "_build_context", lambda *_: current[0])
    stale = regulation_refresh.refresh_regulation_context(object(), original)
    assert stale.context["regulation_interpretation_stale"]
    assert stale.context["ai_facts_fingerprint"] == original.context["ai_facts_fingerprint"]
    current[0] = deepcopy(original.context)
    current[0]["regulation_facts"]["dependency_end"] = "2026-01-02T21:00:00+00:00"
    repaired = regulation_refresh.refresh_regulation_context(object(), stale)
    assert "regulation_interpretation_stale" not in repaired.context
    assert repaired.context["ai_facts_fingerprint"] == report_facts_fingerprint(repaired)


@pytest.mark.parametrize("changed_field", ["metric", "nested_dependency_end", "history_end"])
def test_bounds_migration_cannot_hide_other_evidence_changes(monkeypatch, changed_field: str) -> None:
    original = _bounded_report()
    current = deepcopy(original.context)
    current["regulation_facts"]["dependency_end"] = "2026-01-02T21:00:00+00:00"
    circuit = current["regulation_facts"]["circuits"][0]
    if changed_field == "metric":
        circuit["metrics"]["room_absolute_error_c"]["value"] = 0.8
    elif changed_field == "nested_dependency_end":
        circuit["dependency_end"] = "2026-01-02T21:00:00+00:00"
    else:
        current["regulation_comparisons"]["history_end"] = "2026-01-02T21:00:00+00:00"
    monkeypatch.setattr(regulation_refresh, "_build_context", lambda *_: current)
    stale = regulation_refresh.refresh_regulation_context(object(), original)
    assert stale.context["regulation_interpretation_stale"]
    assert stale.context["ai_facts_fingerprint"] == original.context["ai_facts_fingerprint"]


@pytest.mark.parametrize("baseline", [None, "unknown-old-digest", "facts-v2:unmatched"])
def test_dependency_bounds_cannot_rebase_an_unknown_original_baseline(monkeypatch, baseline) -> None:
    original = _bounded_report()
    original.context["regulation_interpretation_stale"] = True
    if baseline is None:
        original.context.pop("ai_facts_fingerprint")
    else:
        original.context["ai_facts_fingerprint"] = baseline
    current = deepcopy(original.context)
    current["regulation_facts"]["dependency_end"] = "2026-01-02T21:00:00+00:00"
    monkeypatch.setattr(regulation_refresh, "_build_context", lambda *_: current)
    stale = regulation_refresh.refresh_regulation_context(object(), original)
    assert stale.context["regulation_interpretation_stale"]
    assert stale.context.get("ai_facts_fingerprint") == baseline


def test_first_bounds_only_refresh_can_prove_previous_ai_report_without_saved_fingerprint(monkeypatch) -> None:
    original = _bounded_report()
    original.context.pop("ai_facts_fingerprint")
    saved = original.model_dump_json()
    current = deepcopy(original.context)
    current["regulation_facts"]["dependency_end"] = "2026-01-02T21:00:00+00:00"
    monkeypatch.setattr(regulation_refresh, "_build_context", lambda *_: current)
    repaired = regulation_refresh.refresh_regulation_context(object(), original)
    assert "regulation_interpretation_stale" not in repaired.context
    assert repaired.context["ai_facts_fingerprint"] == report_facts_fingerprint(repaired)
    assert original.model_dump_json() == saved


@pytest.mark.parametrize("guard", ["regulation_interpretation_stale", "control_settings_interpretation_stale",
                                   "gas_interpretation_stale", "pilot_ai_reuse", "ai_interpretation_reuse"])
def test_bounds_only_refresh_cannot_establish_unknown_ai_baseline_after_reuse_or_staleness(
    monkeypatch, guard: str,
) -> None:
    original = _bounded_report()
    original.context.pop("ai_facts_fingerprint")
    original.context[guard] = {"facts_changed": None} if guard.endswith("reuse") else True
    current = deepcopy(original.context)
    current["regulation_facts"]["dependency_end"] = "2026-01-02T21:00:00+00:00"
    monkeypatch.setattr(regulation_refresh, "_build_context", lambda *_: current)
    stale = regulation_refresh.refresh_regulation_context(object(), original)
    assert stale.context["regulation_interpretation_stale"]
    assert "ai_facts_fingerprint" not in stale.context


def test_lost_original_bounds_are_not_guessed_during_later_metric_recovery(monkeypatch) -> None:
    original = _bounded_report()
    changed = deepcopy(original.context)
    changed["regulation_facts"]["dependency_end"] = "2026-01-02T21:00:00+00:00"
    changed["regulation_facts"]["circuits"][0]["metrics"]["room_absolute_error_c"]["value"] = 0.8
    current = [changed]
    monkeypatch.setattr(regulation_refresh, "_build_context", lambda *_: current[0])
    stale = regulation_refresh.refresh_regulation_context(object(), original)
    current[0] = deepcopy(changed)
    current[0]["regulation_facts"]["circuits"][0]["metrics"]["room_absolute_error_c"]["value"] = 0.2
    unchanged_baseline = regulation_refresh.refresh_regulation_context(object(), stale)
    assert unchanged_baseline.context["regulation_interpretation_stale"]
    assert unchanged_baseline.context["ai_facts_fingerprint"] == original.context["ai_facts_fingerprint"]
