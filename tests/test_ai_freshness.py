from datetime import UTC, datetime, timedelta

import pytest

from zont_analyzer.application.reasoning_context import original_ai_generated_at
from zont_analyzer.domain import QualityResult, Report
from zont_analyzer.reports import render_html, render_text
from zont_analyzer.reports.presentation import ai_freshness_notice


def report() -> Report:
    start = datetime(2026, 9, 7, tzinfo=UTC)
    return Report(
        id="freshness", kind="daily", period_start=start, period_end=start + timedelta(days=1),
        generated_at=datetime(2026, 9, 8, 0, 16, tzinfo=UTC), timezone="Etc/GMT-4",
        ai_used=True, summary="Сохранённый AI-ответ",
        quality=QualityResult(score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
                              implausible_jumps=0, sample_count=1),
        context={"gas": {"status": "estimated", "ai_stale": True, "volume_m3": 13.8}},
    )


def test_automatic_recalculation_explains_reuse_in_local_time_without_blame() -> None:
    r = report()
    r.context["pilot_ai_reuse"] = {"source_generated_at": "2026-09-08T00:04:03+00:00", "facts_changed": True}
    raw = r.model_dump_json()
    for rendered in (render_html(r), render_text(r)):
        assert rendered.count("Показатели автоматически пересчитаны после обновления данных") == 1
        assert "08.09.2026 04:04" in rendered
        assert "без повторного запроса" in rendered
        assert "AI-интерпретация историческая" not in rendered
        assert "расчёт расхода газа обновлён" not in rendered.lower()
        assert "UTC+4 — Самара, Удмуртия" in rendered
    assert r.model_dump_json() == raw


def test_gas_change_failed_refresh_and_fresh_answer_have_distinct_notices() -> None:
    r = report()
    assert "Расчёт расхода газа обновлён" in ai_freshness_notice(r)
    r.context["ai_interpretation_reuse"] = {"source_generated_at": "bad date"}
    assert "Обновить пояснение AI не удалось" in ai_freshness_notice(r)
    del r.context["ai_interpretation_reuse"]
    r.context["gas"]["ai_stale"] = False
    assert ai_freshness_notice(r) == ""


@pytest.mark.parametrize("key", ["pilot_ai_reuse", "ai_interpretation_reuse"])
def test_original_ai_timestamp_survives_multiple_reuses(key: str) -> None:
    r = report()
    initial = original_ai_generated_at(r)
    r.context[key] = {"source_generated_at": initial}
    r.generated_at += timedelta(minutes=15)
    second = original_ai_generated_at(r)
    r.context = {"pilot_ai_reuse": {"source_generated_at": second}}
    r.generated_at += timedelta(minutes=15)
    assert original_ai_generated_at(r) == initial


def test_unchanged_recalculation_keeps_ai_current_and_original_time() -> None:
    from zont_analyzer.application.reasoning_context import reuse_ai_interpretation

    old = report()
    old.context['gas']['ai_stale'] = False
    fresh = old.model_copy(deep=True)
    fresh.generated_at += timedelta(minutes=15)
    fresh.context['input_revision'] = {'telemetry': 'new revision format'}
    fresh.context['calculation_version'] = 'new implementation'
    fresh.context['gas']['updated'] = True
    retained = reuse_ai_interpretation(old, fresh)
    assert retained.context['pilot_ai_reuse']['facts_changed'] is False
    assert retained.context['gas']['ai_stale'] is False
    assert ai_freshness_notice(retained) == ''
    again = reuse_ai_interpretation(retained, fresh)
    assert original_ai_generated_at(again) == old.generated_at.isoformat()
    assert ai_freshness_notice(again) == ''


def test_changed_facts_stay_stale_across_repeats_and_can_return_to_original() -> None:
    from zont_analyzer.application.reasoning_context import reuse_ai_interpretation

    old = report()
    old.context['gas']['ai_stale'] = False
    fresh = old.model_copy(deep=True)
    fresh.context['gas']['volume_m3'] = 20
    retained = reuse_ai_interpretation(old, fresh)
    assert retained.context['pilot_ai_reuse']['facts_changed'] is True
    assert retained.summary == old.summary
    assert retained.context['gas']['ai_stale'] is True
    assert reuse_ai_interpretation(retained, fresh).context['pilot_ai_reuse']['facts_changed'] is True
    restored = reuse_ai_interpretation(retained, old)
    assert restored.context['pilot_ai_reuse']['facts_changed'] is False
    assert ai_freshness_notice(restored) == ''


def test_legacy_reuse_cannot_claim_proven_change_or_freshness() -> None:
    from zont_analyzer.application.reasoning_context import reuse_ai_interpretation

    old = report()
    old.context['pilot_ai_reuse'] = {'source_generated_at': '2026-09-08T00:04:03+00:00'}
    retained = reuse_ai_interpretation(old, old.model_copy(deep=True))
    assert retained.context['pilot_ai_reuse']['facts_changed'] is None
    notice = ai_freshness_notice(retained)
    assert 'сравнение с исходными данными AI недоступно' in notice
    assert 'после обновления данных' not in notice


def test_fresh_reuse_does_not_hide_later_gas_recalibration() -> None:
    r = report()
    r.context['pilot_ai_reuse'] = {'facts_changed': False}
    assert 'Расчёт расхода газа обновлён' in ai_freshness_notice(r)


def test_ai_metadata_is_not_evidence_and_real_changes_remain_stale() -> None:
    from zont_analyzer.application.reasoning_context import (
        report_facts_fingerprint,
        reuse_ai_interpretation,
    )

    old = report()
    old.context['gas']['ai_stale'] = False
    old.context['ai_provenance'] = {'response_model': 'test-model', 'ai_log_id': 'call:1'}
    old.context['control_settings'] = {'captured_at': '2026-09-08T20:00:00Z', 'parameters': [1]}
    old.context['heating_analysis'] = {'location_captured_at': '2026-09-08T20:00:00Z'}
    old.context['ai_facts_fingerprint'] = report_facts_fingerprint(old)
    fresh = old.model_copy(deep=True)
    del fresh.context['ai_provenance']
    del fresh.context['ai_facts_fingerprint']
    fresh.context['control_settings']['captured_at'] = '2026-09-08T20:08:00Z'
    fresh.context['heating_analysis']['location_captured_at'] = '2026-09-08T20:08:00Z'
    retained = reuse_ai_interpretation(old, fresh)
    assert retained.context['pilot_ai_reuse']['facts_changed'] is False
    assert retained.context['ai_provenance'] == old.context['ai_provenance']
    assert ai_freshness_notice(retained) == ''
    fresh.context['gas']['volume_m3'] = 20
    changed = reuse_ai_interpretation(retained, fresh)
    assert changed.context['pilot_ai_reuse']['facts_changed'] is True
    assert ai_freshness_notice(changed)
    fresh.context['gas']['volume_m3'] = 13.8
    fresh.context['control_settings']['parameters'] = [2]
    assert reuse_ai_interpretation(retained, fresh).context['pilot_ai_reuse']['facts_changed'] is True


def test_legacy_digest_upgrades_only_after_exact_evidence_match() -> None:
    import hashlib
    import json

    from zont_analyzer.application.reasoning_context import reuse_ai_interpretation

    old = report()
    old.context['gas']['ai_stale'] = False
    old.context['ai_provenance'] = {'response_model': 'test-model', 'ai_log_id': 'call:1'}
    # Reproduce the old persisted digest independently of the new implementation.
    payload = old.model_dump(mode='json', include={
        'kind', 'period_start', 'period_end', 'timezone', 'quality', 'metrics', 'events', 'context',
    })
    del payload['context']['gas']['ai_stale']
    baseline = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    old.context['ai_facts_fingerprint'] = baseline
    old.context['pilot_ai_reuse'] = {
        'facts_changed': True, 'source_generated_at': old.generated_at.isoformat(),
    }
    fresh = report()
    fresh.context['gas']['volume_m3'] = 20
    changed = reuse_ai_interpretation(old, fresh)
    assert changed.context['pilot_ai_reuse']['facts_changed'] is None
    assert changed.context['ai_facts_fingerprint'] == baseline
    fresh.context['gas']['volume_m3'] = 13.8
    repaired = reuse_ai_interpretation(changed, fresh)
    assert repaired.context['pilot_ai_reuse']['facts_changed'] is False
    assert repaired.context['ai_facts_fingerprint'].startswith('facts-v2:')
    assert ai_freshness_notice(repaired) == ''
    assert reuse_ai_interpretation(repaired, fresh).context['pilot_ai_reuse']['facts_changed'] is False


def test_completed_daily_analysis_is_stable_after_persistence(tmp_path) -> None:
    from datetime import date

    from zont_analyzer.application.analysis import AnalysisService
    from zont_analyzer.application.reasoning_context import reuse_ai_interpretation
    from zont_analyzer.domain import AnalysisResult
    from zont_analyzer.runtime import build_runtime

    runtime = build_runtime(None, tmp_path)
    runtime.config.home.timezone = 'Europe/Samara'
    runtime.config.analysis.minimum_quality_score = 0
    runtime.config.analysis.daily_ai_when_normal = True
    calls = []

    class Analyst:
        def analyze(self, packet: dict) -> AnalysisResult:
            calls.append(packet)
            return AnalysisResult(summary='Synthetic AI response', provenance={
                'response_model': 'test-model', 'ai_log_id': 'call:1',
            })

    service = AnalysisService(runtime.db, runtime.config, analyst=Analyst())
    first = service.analyze_daily(date(2026, 9, 8))
    second = service.analyze_daily(date(2026, 9, 8), use_ai=False)
    assert first.period_start == datetime(2026, 9, 7, 20, tzinfo=UTC)
    assert first.period_end == datetime(2026, 9, 8, 20, tzinfo=UTC)
    assert (second.period_start, second.period_end) == (first.period_start, first.period_end)
    retained = reuse_ai_interpretation(first, second)
    assert len(calls) == 1
    assert retained.context['pilot_ai_reuse']['facts_changed'] is False
    assert retained.summary == first.summary
    assert ai_freshness_notice(retained) == ''


def test_published_legacy_mismatch_does_not_claim_changed_measurements() -> None:
    r = report()
    r.context['ai_facts_fingerprint'] = 'old-unversioned-digest'
    r.context['pilot_ai_reuse'] = {'facts_changed': True}
    raw = r.model_dump_json()
    for rendered in (render_html(r), render_text(r)):
        assert 'Показатели автоматически пересчитаны после обновления данных' not in rendered
        assert rendered.count('Точное сравнение с исходными данными AI недоступно') == 1
    assert r.model_dump_json() == raw
