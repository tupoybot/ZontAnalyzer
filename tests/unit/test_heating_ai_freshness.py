from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from zont_analyzer.application import regulation_refresh
from zont_analyzer.application.reasoning_context import (
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
