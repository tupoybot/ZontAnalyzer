from datetime import UTC, datetime, timedelta

from zont_analyzer.domain import MetricValue, QualityResult, Report
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
    assert "4,0 ч" in page and "за 24,0 ч периода" in page
    assert "16,7 % от всего периода" in page
    assert "в пробелах работа горелки неизвестна" in page
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


def test_purpose_split_keeps_meter_total_separate_and_unknown_visible() -> None:
    from zont_analyzer.reports.presentation import gas_purpose_text

    report = _report({
        "status": "measured", "scope": "whole_meter", "volume_m3": 5,
        "purpose_split": {
            "status": "estimated", "scope": "shared_meter_model", "total_modelled_m3": 1,
            "unallocated_m3": .1,
            "components": {"heating": {"volume_m3": .6}, "dhw": {"volume_m3": .25},
                           "purpose_unknown": {"volume_m3": .05}},
        },
    })
    lines = gas_purpose_text(report)
    assert "Отопление: 0,60 м³ · 60,0%" in lines
    assert "ГВС: 0,25 м³ · 25,0%" in lines
    assert "Назначение не определено: 0,05 м³ · 5,0%" in lines
    assert "Не распределено из-за пропусков телеметрии: 0,10 м³" in lines
    assert any("их итоги могут отличаться" in line for line in lines)
    assert any("другие газовые потребители не отделены" in line for line in lines)
    assert "Отопление: 0,60 м³ · 60,0%" in render_html(report)
    assert report.context["gas"]["volume_m3"] == 5


def test_partial_purpose_split_does_not_invent_percentages_or_zero() -> None:
    from zont_analyzer.reports.presentation import gas_purpose_text

    report = _report({"purpose_split": {
        "status": "partial", "total_modelled_m3": None, "unallocated_m3": None,
        "components": {"heating": {"volume_m3": .2}, "dhw": {"volume_m3": 0},
                       "purpose_unknown": {"volume_m3": None}},
    }})
    lines = gas_purpose_text(report)
    assert "Отопление: 0,20 м³" in lines
    assert "ГВС: 0,00 м³" in lines
    assert "Назначение не определено: Нет данных" in lines
    assert not any("%" in line for line in lines)


def test_dashboard_uses_short_burner_labels_and_modelled_gas_denominator() -> None:
    from zont_analyzer.reports.presentation import kpis

    report = _report({
        "status": "measured", "scope": "whole_meter", "volume_m3": 5,
        "flame_hours": 4, "heating_flame_hours": 3, "dhw_flame_hours": .5,
        "purpose_split": {
            "status": "estimated", "total_modelled_m3": 1, "unallocated_m3": .1,
            "components": {"heating": {"volume_m3": .6}, "dhw": {"volume_m3": .25},
                           "purpose_unknown": {"volume_m3": .05}},
        },
    })
    report.metrics = [
        MetricValue(id="starts", name="burner_starts", value=12, unit=""),
        MetricValue(id="dhw", name="dhw_episode_count", value=4, unit=""),
        MetricValue(id="zont", name="zont_uptime_seconds", value=86400, unit="", context={"online": True}),
        MetricValue(id="boiler", name="boiler_uptime_seconds", value=7200, unit="", context={"online": False}),
    ]
    dashboard = kpis(report)
    assert "12 запусков · 75% горелки" in dashboard
    assert "30 мин горелки" in dashboard
    assert "времени горения" not in dashboard
    assert dashboard.index("Качество данных") < dashboard.index("Отопление · горелка")
    assert dashboard.index("Отопление · горелка") < dashboard.index("ГВС · догревы")
    assert 'class="gas-distribution-bar"' in dashboard
    assert "Распределение по модели: 1,00 м³" in dashboard
    assert "Показание счётчика и распределение по модели считаются отдельно." in dashboard
    assert "Не определено <b>0,15 м³ · 15%" in dashboard
    assert 'class="kpi-uptime-row" aria-label="Статус и аптаймы"' in dashboard
    assert "ZONT · на связи · аптайм 1 дн." in dashboard
    assert "Котёл · не на связи · аптайм 2 ч" in dashboard


def test_dashboard_does_not_draw_a_gas_bar_without_complete_modelled_parts() -> None:
    from zont_analyzer.reports.presentation import kpis

    report = _report({
        "status": "estimated", "volume_m3": 1,
        "purpose_split": {
            "status": "partial", "total_modelled_m3": None, "unallocated_m3": None,
            "components": {"heating": {"volume_m3": .2}, "dhw": {"volume_m3": 0},
                           "purpose_unknown": {"volume_m3": None}},
        },
    })
    dashboard = kpis(report)
    assert 'class="gas-distribution-bar"' not in dashboard
    assert "Распределение по модели: Нет данных" in dashboard
