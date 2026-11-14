"""Select useful observations instead of exhaustively rendering every fact."""
from copy import deepcopy
from datetime import UTC, datetime

from zont_analyzer.domain import QualityResult, Report
from zont_analyzer.reports.regulation import regulation_observations


def _report(context: dict) -> Report:
    return Report(id="fixture", kind="weekly", period_start=datetime(2026, 9, 1, tzinfo=UTC),
                  period_end=datetime(2026, 9, 8, tzinfo=UTC), generated_at=datetime(2026, 9, 8, tzinfo=UTC),
                  context=context, summary="Синтетический отчёт.", quality=QualityResult(
                      score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
                      implausible_jumps=0, sample_count=20))


def _text(context: dict) -> str:
    return " ".join(item.statement for item in regulation_observations(_report(context)))


def _comparison() -> dict:
    return {"stage": "available", "outcome": "mixed", "event_id": "change-current",
            "event": {"event_id": "change-current", "boundary": {
                "after": "2026-09-03T10:00:00+00:00", "no_later_than": "2026-09-03T12:00:00+00:00"},
                "changed_values": {
                    "pid_prop_koef": {"before": {"status": "observed", "value": 5},
                                      "after": {"status": "observed", "value": 3}},
                    "pid_integral_koef": {"before": {"status": "observed", "value": 2},
                                          "after": {"status": "observed", "value": 4}}}},
            "before": {"useful_hours": 12}, "after": {"useful_hours": 14},
            "metrics": {"room_absolute_error_c": {"before": .4, "after": .2},
                        "actual_variation_c_per_hour": {"before": 4, "after": 8}}}


def test_mixed_comparison_preserves_both_quantities_and_joint_change() -> None:
    context = {"regulation_comparisons": {"results": [_comparison()]}}
    before = deepcopy(context)
    text = _text(context)
    assert context == before
    assert "P 5 → 3" in text and "I 2 → 4" in text
    assert "разнонаправлен" in text
    assert "абсолютное отклонение" in text and "0,4 → 0,2 °C" in text
    assert "измеренной подачи 4 → 8 °C/ч" in text
    assert "точное время неизвестно" in text
    assert "не доказывает" in text
    assert "отдельного параметра" in text


def test_null_metrics_and_unrecognized_codes_do_not_expand_observations() -> None:
    metrics = {"room_absolute_error_c": {"value": .25, "coverage_pct": 60}}
    context = {"regulation_facts": {"circuits": [{"metrics": metrics}]}}
    original = _text(context)
    metrics.update({f"future_metric_{index}": {"value": None, "unit": "°C"} for index in range(50)})
    context["regulation_facts"]["circuits"][0]["unknowns"] = ["future_internal_code"] * 30
    assert _text(context) == original
    assert "нет данных °C" not in original


def test_different_metric_coverage_is_retained_without_claiming_whole_period() -> None:
    text = _text({"regulation_facts": {"circuits": [{"metrics": {
        "room_absolute_error_c": {"value": .25, "coverage_pct": 60},
        "actual_range_c": {"value": 12, "coverage_pct": 40},
        "command_range_c": {"value": 3, "coverage_pct": 80},
    }}]}})
    assert "60%" in text and "40%" in text and "80%" in text
    assert "12 °C" in text and "3 °C" in text
    assert "измеренной" in text and "заданной" in text


def test_unavailable_comparison_does_not_show_invalid_partial_metric_deltas() -> None:
    comparison = _comparison()
    comparison.update(stage="insufficient", outcome=None,
                      next_observation=["observe_matching_weather_target_and_mode"])
    text = _text({"regulation_comparisons": {"results": [comparison]}})
    assert "сопоставим" in text and "погод" in text
    assert "0,4 → 0,2" not in text and "4 → 8" not in text
    assert "улучшение" not in text


def test_recovery_is_observed_not_assumed_and_many_windows_are_aggregated() -> None:
    window = {"reason": "abrupt_room_cooling", "started_at": "2026-09-03T10:00:00+00:00",
              "ended_at": "2026-09-03T11:00:00+00:00", "recovered": True}
    context = {"regulation_facts": {"circuits": [{"exclusion_windows": [window]}]}}
    text = _text(context)
    assert "неустановленной причиной" in text
    assert "Восстановление наблюдалось" in text and "11:00" in text
    window["recovered"] = False
    assert "ещё не подтверждено" in _text(context)
    context["regulation_facts"]["circuits"][0]["exclusion_windows"] = [window] * 50
    assert len(_text(context).split()) < 60


def test_legacy_reports_without_regulation_context_get_no_new_observation() -> None:
    assert regulation_observations(_report({})) == []


def test_sampled_exclusions_are_not_reported_as_the_episode_census() -> None:
    text = _text({"regulation_facts": {"circuits": [{
        "exclusion_windows_total": 60, "exclusion_windows_truncated": True,
        "exclusion_windows": [{"reason": "dhw", "recovered": True}] * 12,
    }]}})
    assert "Среди исключённых участков" in text
    assert "12 эпизодов" not in text and "60 эпизодов" not in text
    assert "показанных эпизодах" in text


def test_available_metrics_do_not_hide_missing_days() -> None:
    text = _text({"regulation_facts": {"circuits": [{"incomplete": True,
        "metrics": {"room_absolute_error_c": {"value": .2, "coverage_pct": 80}},
    }]}})
    assert "0,2 °C" in text and "Не все сутки рассчитаны" in text


def test_short_fact_coverage_refers_to_its_interval_not_the_whole_report() -> None:
    report = _report({"regulation_facts": {"incomplete": True, "circuits": [{
        "period_start": "2026-09-03T10:00:00+00:00", "period_end": "2026-09-03T12:00:00+00:00",
        "metrics": {"room_absolute_error_c": {"value": .2, "coverage_pct": 100}},
    }]}})
    observation = regulation_observations(report)[0]
    assert observation.interval is not None
    assert (observation.interval.ended_at - observation.interval.started_at).total_seconds() == 7200
    assert observation.interval.started_at > report.period_start
    assert "100% указанного интервала" in observation.statement
