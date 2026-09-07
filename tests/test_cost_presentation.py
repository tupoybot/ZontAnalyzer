from datetime import UTC, datetime, timedelta

import pytest

from zont_analyzer.domain import QualityResult, Report
from zont_analyzer.reports import render_html, render_text


def _cost(amount: str, *, status: str = "available", currency: str = "RUB") -> dict:
    return {
        "status": status,
        "amounts": [{"currency": currency, "amount": amount}],
        "coverage_pct": 100.0 if status == "available" else 50.0,
        "priced_volume_m3": 1.0,
        "unpriced_volume_m3": 0.0 if status == "available" else 1.0,
        "currency_status": "single",
        "basis": "calendar_month_tariffs",
        "slices": [],
        "limitations": [],
    }


def _report(kind: str = "daily") -> Report:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    gas_cost = _cost("8.01")
    return Report(
        id=f"cost-{kind}", kind=kind, period_start=start, period_end=start + timedelta(days=1),
        generated_at=start, summary="Период обработан", timezone="UTC",
        quality=QualityResult(score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
                              implausible_jumps=0, sample_count=1),
        context={"gas": {
            "status": "measured", "scope": "whole_meter", "volume_m3": 1.0,
            "reliability_index_pct": 100, "cost": gas_cost,
        }},
    )


@pytest.mark.parametrize("kind", ["daily", "weekly", "monthly", "seasonal"])
def test_money_is_adjacent_to_volume_in_html_and_text_for_every_report_kind(kind: str) -> None:
    report = _report(kind)

    for rendered in (render_html(report), render_text(report)):
        assert "1,00 м³ · 8,01 руб." in rendered
        assert "оценочная стоимость" not in rendered.lower()
        assert "счёт на оплату" not in rendered.lower()


def test_partial_and_unknown_costs_are_explicit_and_never_fabricated() -> None:
    partial = _report()
    partial.context["gas"]["volume_m3"] = 2.0
    partial.context["gas"]["cost"] = _cost("8.01", status="partial")
    unknown = _report()
    unknown.context["gas"]["cost"] = {
        "status": "unknown", "amounts": [], "coverage_pct": 0,
        "priced_volume_m3": 0, "unpriced_volume_m3": 1,
        "currency_status": "none", "basis": "calendar_month_tariffs", "slices": [],
        "limitations": ["tariff_unavailable"],
    }

    partial_page = render_html(partial)
    assert "2,00 м³ · 8,01 руб. (частично)" in partial_page
    assert "с тарифом 1,00 м³; без тарифа 1,00 м³" in partial_page
    unknown_page = render_html(unknown)
    assert "1,00 м³" in unknown_page
    assert "· Стоимость неизвестна" not in unknown_page
    assert "для расхода нет действующего тарифа" in unknown_page
    assert "0,00 руб." not in unknown_page


def test_mixed_currencies_stay_separate_and_month_slices_show_basis() -> None:
    report = _report("monthly")
    report.context["gas"]["volume_m3"] = 3.0
    report.context["gas"]["cost"] = {
        "status": "available",
        "amounts": [{"currency": "RUB", "amount": "8.01"}, {"currency": "USD", "amount": "2.00"}],
        "coverage_pct": 100, "priced_volume_m3": 3, "unpriced_volume_m3": 0,
        "currency_status": "mixed", "basis": "calendar_month_tariffs", "timezone": "Europe/Samara",
        "slices": [
            {"start": "2026-07-31T20:00:00+00:00", "end": "2026-08-31T20:00:00+00:00",
             "volume_m3": 1, "price_per_m3": "8.01", "currency": "RUB", "amount": "8.01"},
            {"start": "2026-08-31T20:00:00+00:00", "end": "2026-09-30T20:00:00+00:00",
             "volume_m3": 2, "price_per_m3": "1.00", "currency": "USD", "amount": "2.00"},
        ],
    }

    page = render_html(report)
    assert "8,01 руб. + 2,00 USD" in page
    assert "Стоимость по календарным месяцам" in page
    assert "2026-08: 1,00 м³ · 8,01 руб.; тариф 8,01 руб./м³" in page
    assert "2026-09: 2,00 м³ · 2,00 USD; тариф 1,00 USD/м³" in page


def test_distribution_omits_money_while_period_and_savings_keep_it() -> None:
    report = _report("weekly")
    report.context["gas"].update({
        "average_daily_m3": .5, "average_daily_cost": _cost("4.01"),
        "average_weekly_m3": 3.5, "average_weekly_cost": _cost("28.04"),
        "measured_intervals": [{
            "start": "2026-07-01", "end": "2026-07-02", "volume_m3": 1,
            "cost": _cost("8.01"),
        }],
    })
    report.context["gas"]["purpose_split"] = {
        "status": "estimated", "total_modelled_m3": 1.0, "unallocated_m3": 0.0,
        "cost": _cost("8.01"), "unallocated_cost": _cost("0.00"),
        "components": {
            "heating": {"volume_m3": .75, "cost": _cost("6.01")},
            "dhw": {"volume_m3": .25, "cost": _cost("2.00")},
            "purpose_unknown": {"volume_m3": 0.0, "cost": _cost("0.00")},
        },
    }
    report.context["gas_savings"] = {
        "status": "available",
        "comparisons": [{
            "before_start": "2026-07-01", "after_start": "2026-08-01",
            "raw_savings": {"m3": 1, "pct": 10},
            "normalized_savings": {"m3": 1, "pct": 10},
            "normalized_savings_cost": _cost("8.01"),
            "actual_costs": {"before": _cost("80.10"), "after": _cost("72.09")},
            "effect_status": "estimated", "provenance": {},
        }],
    }

    from zont_analyzer.reports.presentation import gas_distribution_card, gas_purpose_text

    purpose = " ".join(gas_purpose_text(report))
    dashboard = gas_distribution_card(report.context["gas"])
    distribution = dashboard[dashboard.index('<div class="gas-distribution">'):]
    assert "руб." not in purpose
    assert "руб." not in distribution
    assert "1,00 м³ · 8,01 руб." in dashboard

    for rendered in (render_html(report), render_text(report)):
        assert "Отопление: 0,75 м³" in rendered
        assert "ГВС: 0,25 м³" in rendered
        assert "Расход по модели за период: 1,00 м³" in rendered
        assert "0,50 м³/сутки · 4,01 руб." in rendered
        assert "3,50 м³/неделю · 28,04 руб." in rendered
        assert "2026-07-01" in rendered and "1,00 м³ · 8,01 руб." in rendered
        assert "Нормализованное изменение: 1,00 м³ · 8,01 руб." in rendered
        assert "Фактическая стоимость: до 80,10 руб.; после 72,09 руб." in rendered
        assert "в тарифах периода после изменения" in rendered


def test_actual_period_cost_comparison_is_separate_from_gas_savings() -> None:
    report = _report("monthly")
    report.context["period"] = {
        "start": report.period_start.isoformat(), "end": report.period_end.isoformat(),
        "observed_end": report.period_end.isoformat(), "complete": True,
    }
    report.context["period_comparisons"] = [{
        "label": "Предыдущий месяц", "unavailable_reason": "Нет сопоставимых суток",
        "gas_cost_comparison": {
            "before": _cost("70.00"), "after": _cost("80.10"), "actual_change": _cost("10.10"),
        },
    }]

    page = render_html(report)
    text = render_text(report)
    for rendered in (page, text):
        assert "Предыдущий месяц: до 70,00 руб.; после 80,10 руб.; изменение 10,10 руб." in rendered
    assert "Фактические суммы рассчитаны по тарифам каждого периода" in page


def test_cost_layout_wraps_on_desktop_and_stacks_on_mobile() -> None:
    page = render_html(_report())
    assert ".gas-kpi-total strong{display:block;font-size:clamp" in page
    assert "overflow-wrap:anywhere" in page
    assert "@media(max-width:720px)" in page
    assert ".kpi-gas-strip{grid-template-columns:1fr" in page


def test_matched_volume_effect_is_distinct_from_actual_money_change() -> None:
    from zont_analyzer.reports.presentation import gas_cost_comparisons_text

    report = _report()
    report.context["period_comparisons"] = [{
        "label": "Предыдущая неделя",
        "gas_cost_comparison": {"before": _cost("8"), "after": _cost("9"), "actual_change": _cost("1")},
        "matched_windows": [{"gas_cost_comparison": {
            "volume_effect_cost": {**_cost("4.5"), "volume_m3": .5},
        }}, {"gas_cost_comparison": {
            "volume_effect_cost": {**_cost("999", status="unknown"), "volume_m3": 99},
        }}],
    }]
    text = " ".join(gas_cost_comparisons_text(report))
    assert "изменение 1,00 руб." in text
    assert "0,50 м³ · 4,50 руб." in text
    assert "в тарифах оцениваемых суток" in text
    assert "999" not in text
