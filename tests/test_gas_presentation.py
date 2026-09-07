from datetime import UTC, datetime, timedelta

from zont_analyzer.domain import QualityResult, Report
from zont_analyzer.reports import render_html


def _report(gas: dict, kind: str = "daily") -> Report:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return Report(
        id="gas-report", kind=kind, period_start=start, period_end=start + timedelta(days=1),
        generated_at=start, summary="Период обработан", context={"gas": gas},
        quality=QualityResult(score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
                              implausible_jumps=0, sample_count=1),
    )


def test_gas_card_renders_measurement_provenance_and_flame_denominator() -> None:
    page = render_html(_report({
        "status": "estimated", "volume_m3": 12.3, "lower_m3": 10, "upper_m3": 15,
        "reliability_index_pct": 72, "coverage_pct": 88, "model_version": "gas-1",
        "observed_days": 1, "observed_hours": 20, "flame_hours": 4, "flame_pct": 20,
        "scope": "boiler", "reasons": ["неполный интервал"], "complete": False,
    }))
    assert "Расход газа за период" in page
    assert "12,3 м³" in page
    assert "Индекс надёжности: 72,0 %" in page
    assert "Знаменатель: 1 календарных суток" in page
    assert "Время работы горелки" in page
    assert "4,0 ч" in page and "за 20 ч наблюдений" in page
    assert "Период неполный" in page
    assert "Модель: gas-1" in page


def test_unknown_gas_does_not_look_like_zero_and_stale_ai_is_visible() -> None:
    page = render_html(_report({"status": "unknown", "volume_m3": 0, "ai_stale": True, "flame_hours": 0}))
    assert "Расход газа за период" in page
    assert "Нет данных" in page
    assert "Объяснение AI устарело" in page
    assert "0,0 ч" in page


def test_question_control_is_readable_and_aligned() -> None:
    page = render_html(_report({}))
    assert 'class="counterfactual-question"' in page
    assert 'placeholder="Необязательно.' in page
    assert '.report-regeneration{display:grid' in page
    assert '.counterfactual-question{display:block;width:100%' in page


def test_gas_savings_shows_normalized_range_and_keeps_causality_qualified() -> None:
    report = _report({"status": "measured", "volume_m3": 20})
    report.context["gas_savings"] = {
        "status": "available",
        "comparisons": [{
            "intervention_id": "setting-1",
            "before_start": "2026-01-01T00:00:00Z", "after_start": "2026-01-08T00:00:00Z",
            "raw_savings": {"m3": 3, "pct": 8},
            "normalized_savings": {"m3": -1, "upper_m3": 4, "pct": 2},
            "uncertainty_m3": 2, "effect_status": "неразличим",
        }],
    }
    page = render_html(report)
    assert "Экономия газа" in page
    assert "Диапазон эффекта: -3,0 м³ — 1,0 м³" in page
    assert "Причинность по одному сравнению не доказана" in page
