from datetime import UTC, datetime

import pytest

from zont_analyzer.application.gas_cost import (
    calculate_gas_cost,
    format_cost,
    subtract_costs,
    value_volume_by_period_tariffs,
)
from zont_analyzer.domain import QualityResult, Report
from zont_analyzer.runtime import build_runtime

START = datetime(2026, 1, 1, tzinfo=UTC)
FEBRUARY = datetime(2026, 2, 1, tzinfo=UTC)
END = datetime(2026, 3, 1, tzinfo=UTC)


def tariff(identifier: str, price: str, currency: str, effective_from: datetime) -> dict[str, str]:
    return {
        "id": identifier,
        "price": price,
        "currency": currency,
        "effective_from": effective_from.isoformat(),
    }


def test_decimal_cost_and_zero_tariff_are_exact() -> None:
    january = [tariff("jan", "8.01", "RUB", START)]
    one = calculate_gas_cost(START, FEBRUARY, 1, january, timezone="UTC")
    ten = calculate_gas_cost(START, FEBRUARY, 10, january, timezone="UTC")
    zero = calculate_gas_cost(
        START, FEBRUARY, 10, [tariff("zero", "0", "RUB", START)], timezone="UTC",
    )

    assert one["amounts"] == [{"currency": "RUB", "amount": "8.01"}]
    assert ten["amounts"] == [{"currency": "RUB", "amount": "80.1"}]
    assert zero["status"] == "available"
    assert zero["amounts"] == [{"currency": "RUB", "amount": "0"}]


def test_tariff_boundary_requires_existing_volume_distribution() -> None:
    history = [tariff("jan", "8.01", "RUB", START), tariff("feb", "9", "RUB", FEBRUARY)]
    missing = calculate_gas_cost(START, END, 3, history, timezone="UTC")
    priced = calculate_gas_cost(
        START,
        END,
        3,
        history,
        timezone="UTC",
        volume_slices=[
            {"start": START, "end": FEBRUARY, "volume_m3": 1, "allocation": "gas_model"},
            {"start": FEBRUARY, "end": END, "volume_m3": 2, "allocation": "gas_model"},
        ],
    )

    assert missing["status"] == "unknown"
    assert missing["limitations"] == ["volume_distribution_unavailable"]
    assert priced["status"] == "available"
    assert priced["amounts"] == [{"currency": "RUB", "amount": "26.01"}]


def test_missing_tariff_is_partial_and_currency_changes_are_never_summed() -> None:
    history = [tariff("feb", "9", "RUB", FEBRUARY)]
    partial = calculate_gas_cost(
        START,
        END,
        3,
        history,
        timezone="UTC",
        volume_slices=[
            {"start": START, "end": FEBRUARY, "volume_m3": 1},
            {"start": FEBRUARY, "end": END, "volume_m3": 2},
        ],
    )
    mixed = calculate_gas_cost(
        START,
        END,
        3,
        [tariff("jan", "8", "RUB", START), tariff("feb", "2", "USD", FEBRUARY)],
        timezone="UTC",
        volume_slices=[
            {"start": START, "end": FEBRUARY, "volume_m3": 1},
            {"start": FEBRUARY, "end": END, "volume_m3": 2},
        ],
    )

    assert partial["status"] == "partial"
    assert partial["amounts"] == [{"currency": "RUB", "amount": "18"}]
    assert partial["unpriced_volume_m3"] == 1
    assert mixed["currency_status"] == "mixed"
    assert mixed["amounts"] == [
        {"currency": "RUB", "amount": "8"},
        {"currency": "USD", "amount": "4"},
    ]
    assert subtract_costs(mixed, mixed)["status"] == "unknown"


def test_slices_must_be_bounded_contiguous_and_conserve_canonical_volume() -> None:
    history = [tariff("jan", "8", "RUB", START), tariff("feb", "9", "RUB", FEBRUARY)]
    mismatched = calculate_gas_cost(
        START,
        END,
        3,
        history,
        timezone="UTC",
        volume_slices=[
            {"start": START, "end": FEBRUARY, "volume_m3": 1},
            {"start": FEBRUARY, "end": END, "volume_m3": 3},
        ],
    )
    crossing = calculate_gas_cost(
        START,
        END,
        3,
        history,
        timezone="UTC",
        volume_slices=[{"start": START, "end": END, "volume_m3": 3}],
    )

    assert mismatched["limitations"] == ["volume_slices_do_not_match_total"]
    assert crossing["limitations"] == ["volume_slice_crosses_tariff_boundary"]


def test_effect_uses_evaluated_period_weights_and_formatting_is_shared() -> None:
    evaluated = calculate_gas_cost(
        START,
        END,
        3,
        [tariff("jan", "8.01", "RUB", START), tariff("feb", "9", "RUB", FEBRUARY)],
        timezone="UTC",
        volume_slices=[
            {"start": START, "end": FEBRUARY, "volume_m3": 1},
            {"start": FEBRUARY, "end": END, "volume_m3": 2},
        ],
    )
    effect = value_volume_by_period_tariffs(3, evaluated)

    assert effect["basis"] == "evaluated_period_tariff_weights"
    assert effect["amounts"] == [{"currency": "RUB", "amount": "26.01"}]
    assert format_cost(effect) == "26,01 руб."
    assert format_cost({**effect, "status": "partial"}) == "26,01 руб. (частично)"
    assert format_cost(None) == "Стоимость неизвестна"


def test_timezone_boundary_uses_utc_instant_of_local_month() -> None:
    samara_month = datetime(2026, 1, 31, 20, tzinfo=UTC)
    history = [tariff("feb", "8.01", "RUB", samara_month)]
    before = calculate_gas_cost(
        datetime(2026, 1, 31, 19, tzinfo=UTC), samara_month, 1, history, timezone="Europe/Samara",
    )
    after = calculate_gas_cost(
        samara_month, datetime(2026, 1, 31, 21, tzinfo=UTC), 1, history, timezone="Europe/Samara",
    )

    assert before["status"] == "unknown"
    assert after["amounts"] == [{"currency": "RUB", "amount": "8.01"}]


def test_formatter_handles_unsupported_extreme_amount_without_crashing() -> None:
    assert format_cost({"status": "available", "amounts": [{"currency": "RUB", "amount": "1e1000"}]}) == (
        "Стоимость неизвестна"
    )


def test_invalid_period_is_rejected() -> None:
    with pytest.raises(ValueError, match="after start"):
        calculate_gas_cost(START, START, 1, [], timezone="UTC")


def test_refresh_cost_reads_stored_baseline_inside_database_session(tmp_path) -> None:
    from zont_analyzer.application.gas import GasService
    from zont_analyzer.application.gas_tariffs import GasTariffStore

    runtime = build_runtime(None, tmp_path)
    GasTariffStore(runtime.db, "UTC").save(
        {"price": "8.01", "currency": "RUB", "effective_month": "2026-01"}
    )
    quality = QualityResult(
        score=1,
        coverage_pct=100,
        max_gap_seconds=0,
        stuck_pct=0,
        implausible_jumps=0,
        sample_count=1,
    )
    baseline = Report(
        id="baseline",
        kind="daily",
        period_start=START,
        period_end=datetime(2026, 1, 2, tzinfo=UTC),
        generated_at=datetime(2026, 1, 3, tzinfo=UTC),
        quality=quality,
        summary="baseline",
        context={"gas": {"status": "estimated", "volume_m3": 2}},
    )
    runtime.db.save_report(baseline, baseline.summary)
    current_start = datetime(2026, 1, 2, tzinfo=UTC)
    current_end = datetime(2026, 1, 3, tzinfo=UTC)
    current = Report(
        id="current",
        kind="daily",
        period_start=current_start,
        period_end=current_end,
        generated_at=datetime(2026, 1, 4, tzinfo=UTC),
        quality=quality,
        summary="current",
        context={
            "gas": {"status": "estimated", "volume_m3": 1},
            "period_comparisons": [{
                "matched_windows": [{
                    "status": "comparable",
                    "before_start": START.isoformat(),
                    "before_end": baseline.period_end.isoformat(),
                    "after_start": current_start.isoformat(),
                    "after_end": current_end.isoformat(),
                }],
            }],
        },
    )

    refreshed = GasService(runtime.db, runtime.config).refresh_cost(current)
    comparison = refreshed.context["period_comparisons"][0]["matched_windows"][0]["gas_cost_comparison"]

    assert comparison["before"]["amounts"] == [{"currency": "RUB", "amount": "16.02"}]
    assert comparison["after"]["amounts"] == [{"currency": "RUB", "amount": "8.01"}]
    assert comparison["volume_effect_cost"]["amounts"] == [{"currency": "RUB", "amount": "8.01"}]
    assert GasService(runtime.db, runtime.config).refresh_cost(refreshed).context == refreshed.context


def test_refresh_cost_preserves_gas_and_ai_and_does_not_scan_without_tariffs(tmp_path, monkeypatch) -> None:
    from zont_analyzer.application.gas import GasService

    runtime = build_runtime(None, tmp_path)
    service = GasService(runtime.db, runtime.config)
    monkeypatch.setattr(service, "window", lambda *_args, **_kwargs: pytest.fail("unexpected telemetry scan"))
    report = Report(
        id="no-tariff",
        kind="daily",
        period_start=START,
        period_end=datetime(2026, 1, 2, tzinfo=UTC),
        generated_at=datetime(2026, 1, 3, tzinfo=UTC),
        quality=QualityResult(
            score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0, implausible_jumps=0, sample_count=1,
        ),
        summary="AI summary remains byte-for-byte",
        ai_used=True,
        context={
            "gas": {
                "status": "estimated", "volume_m3": 1.25, "model_version": "keep-me",
                "purpose_split": {"total_modelled_m3": 1.25, "components": {}},
            },
        },
    )

    refreshed = service.refresh_cost(report)

    assert refreshed.summary == report.summary and refreshed.ai_used is True
    assert refreshed.context["gas"]["volume_m3"] == 1.25
    assert refreshed.context["gas"]["model_version"] == "keep-me"
    assert refreshed.context["gas"]["cost"]["status"] == "unknown"


def test_currency_change_suppresses_comparable_window_money_effect(tmp_path) -> None:
    from zont_analyzer.application.gas import GasService
    from zont_analyzer.application.gas_tariffs import GasTariffStore

    runtime = build_runtime(None, tmp_path)
    tariffs = GasTariffStore(runtime.db, "UTC")
    tariffs.save({"price": "8", "currency": "RUB", "effective_month": "2026-01"})
    tariffs.save({"price": "2", "currency": "USD", "effective_month": "2026-02"})
    quality = QualityResult(
        score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0, implausible_jumps=0, sample_count=1,
    )
    january = Report(
        id="january",
        kind="monthly",
        period_start=START,
        period_end=FEBRUARY,
        generated_at=FEBRUARY,
        quality=quality,
        summary="January",
        context={"gas": {"status": "estimated", "volume_m3": 10}},
    )
    runtime.db.save_report(january, january.summary)
    march = Report(
        id="february",
        kind="monthly",
        period_start=FEBRUARY,
        period_end=END,
        generated_at=END,
        quality=quality,
        summary="February",
        context={
            "gas": {"status": "estimated", "volume_m3": 8},
            "period_comparisons": [{
                "matched_windows": [{
                    "status": "comparable",
                    "before_start": START.isoformat(),
                    "before_end": FEBRUARY.isoformat(),
                    "after_start": FEBRUARY.isoformat(),
                    "after_end": END.isoformat(),
                }],
            }],
        },
    )

    refreshed = GasService(runtime.db, runtime.config).refresh_cost(march)
    comparison = refreshed.context["period_comparisons"][0]["matched_windows"][0]["gas_cost_comparison"]

    assert comparison["before"]["amounts"] == [{"currency": "RUB", "amount": "80"}]
    assert comparison["after"]["amounts"] == [{"currency": "USD", "amount": "16"}]
    assert comparison["actual_change"]["limitations"] == ["currencies_not_comparable"]
    assert comparison["volume_effect_cost"]["status"] == "unknown"
