from datetime import UTC, datetime, timedelta

import pytest

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
    assert "12,30 м³" in page
    assert 'class="metric-group gas-period-card"' in page
    assert page.index('id="metrics"') < page.index('id="gas-period-title"')
    assert "Проверка модели: недостаточно данных" in page
    assert "Индекс надёжности" not in page
    assert "(надёжность 72%)" not in page
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
    assert "Расчёт расхода газа обновлён после AI-ответа" in page
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
    assert "Диапазон эффекта: -3,00 м³ — 1,00 м³" in page
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
        "reliability_index_pct": 72,
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
    assert "12 запусков · 75%" in dashboard
    assert "30 мин · 12,5%" in dashboard
    assert "(по счётчику)" in dashboard
    assert "надёжность 72%" not in dashboard
    assert "времени горения" not in dashboard
    assert dashboard.index("Качество данных") < dashboard.index("Отопление · горелка")
    assert dashboard.index("Отопление · горелка") < dashboard.index("ГВС · догревы")
    assert 'class="gas-distribution-bar"' in dashboard
    assert "Распределение расхода</span>" in dashboard
    assert "показание счётчика учитывается отдельно" not in dashboard
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


@pytest.mark.parametrize("error, expected", [(0, "0%"), (.09, "9%"), (1.37, "137%")])
def test_validation_error_is_shared_by_kpi_details_and_plaintext(error: float, expected: str) -> None:
    from zont_analyzer.reports.gas_validation import validation_lines
    from zont_analyzer.reports.presentation import gas_distribution_card
    from zont_analyzer.reports.renderers import render_text

    gas = {
        "status": "estimated", "volume_m3": 5, "reliability_index_pct": 45,
        "model": {"validation_relative_error": error, "heldout_interval_count": 2,
                  "validation_error_m3": 0, "usable_interval_count": 8},
    }
    report = _report(gas)
    kpi, page, text = gas_distribution_card(gas), render_html(report), render_text(report)
    for output in (kpi, page, text):
        assert f"ошибка проверки {expected} · интервалов: 2" in output
        assert "Индекс надёжности" not in output
        assert "(надёжность 45%)" not in output
    for line in validation_lines(gas):
        assert line in page and line in text
    assert "Средняя абсолютная ошибка за проверочный интервал: 0 м³" in text
    assert "Пригодных интервалов для калибровки: 8" in text
    assert "целых интервалах" in kpi
    assert "заранее исключили из обучения" in text
    assert "не гарантирует точность распределения расхода по отдельным дням" in text


@pytest.mark.parametrize("model", [
    None, {}, {"validation_relative_error": .09},
    {"validation_relative_error": .09, "heldout_interval_count": 0},
    *[{"validation_relative_error": value, "heldout_interval_count": 2}
      for value in (-1, True, float("nan"), float("inf"))],
    *[{"validation_relative_error": 0, "heldout_interval_count": count}
      for count in (-1, True, .5, float("nan"), float("inf"))],
])
def test_invalid_or_missing_validation_never_uses_legacy_score(model: object) -> None:
    from zont_analyzer.reports.gas_validation import validation_lines, validation_summary

    gas = {"model": model, "reliability_index_pct": 45}
    assert validation_summary(gas) == "Проверка модели: недостаточно данных"
    assert validation_lines(gas)[0] == "Проверка модели: недостаточно данных"
    assert not any("45" in line or "nan" in line or "inf" in line for line in validation_lines(gas))


def test_measured_kpi_separates_whole_meter_total_from_model_check() -> None:
    from zont_analyzer.reports.presentation import gas_distribution_card
    from zont_analyzer.reports.renderers import render_text

    gas = {
        "status": "measured", "scope": "shared_meter", "volume_m3": 5,
        "measurement_time_precision": "day",
        "model": {"validation_relative_error": .09, "heldout_interval_count": 2,
                  "boundary_relative_uncertainty": .8},
    }
    kpi = gas_distribution_card(gas)
    assert "(по счётчику)" in kpi
    assert "ошибка проверки" not in kpi
    assert "Проверка модели приведена в подробностях" in kpi
    for output in (render_html(_report(gas)), render_text(_report(gas))):
        assert "ошибка проверки 9% · интервалов: 2" in output
        assert "не отделяет расход котла от других газовых потребителей" in output
        assert "Время снятия показаний внутри дня неизвестно" in output
        assert "оно не является ошибкой проверки модели" in output
        assert "45%" not in output


def test_invalid_optional_validation_values_are_omitted() -> None:
    from zont_analyzer.reports.gas_validation import validation_lines

    lines = validation_lines({"model": {
        "validation_relative_error": 0, "heldout_interval_count": 1,
        "validation_error_m3": float("nan"), "usable_interval_count": True,
        "boundary_relative_uncertainty": -1,
    }})
    assert not any("Средняя абсолютная" in line or "Пригодных" in line or "Время снятия" in line for line in lines)


def test_legacy_ai_text_has_a_notice_without_changing_saved_interpretation() -> None:
    from zont_analyzer.reports.presentation import ai_freshness_notice

    report = _report({"reliability_index_pct": 45})
    assert ai_freshness_notice(report) == ""
    report.ai_used = True
    original = report.model_dump_json()
    assert "прежний условный индекс" in ai_freshness_notice(report)
    assert report.model_dump_json() == original


def test_validation_without_relative_error_can_show_absolute_error() -> None:
    from zont_analyzer.reports.gas_validation import validation_lines

    lines = validation_lines({"model": {
        "validation_relative_error": None, "validation_error_m3": 1.2, "heldout_interval_count": 2,
    }})
    assert "ошибка проверки 1,2 м³ за интервал · интервалов: 2" in lines[0]
    assert any("Относительная ошибка проверки не определена" in line for line in lines)
