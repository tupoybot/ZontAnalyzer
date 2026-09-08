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
    r.context["pilot_ai_reuse"] = {"source_generated_at": "2026-09-08T00:04:03+00:00"}
    raw = r.model_dump_json()
    for rendered in (render_html(r), render_text(r)):
        assert "Показатели автоматически пересчитаны после обновления данных" in rendered
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
