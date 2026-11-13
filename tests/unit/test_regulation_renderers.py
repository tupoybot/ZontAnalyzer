from copy import deepcopy

import pytest

from zont_analyzer.analytics.regulation import ALGORITHM_VERSION
from zont_analyzer.reports.regulation import regulation_html, regulation_text


def _context() -> dict:
    return {
        "regulation_comparisons": {"results": [{
            "stage": "available", "outcome": "mixed",
            "event": {"boundary": {"after": "2026-09-01T12:00:00+00:00",
                                   "no_later_than": "2026-09-01T14:00:00+00:00"},
                      "changed_values": {"pid_prop_koef": {
                          "before": {"status": "observed", "value": 5},
                          "after": {"status": "observed", "value": 3}}}},
            "before": {"useful_hours": 12, "complete_cycles": 5},
            "after": {"useful_hours": 14, "complete_cycles": 6},
            "metrics": {
                "room_absolute_error_c": {"before": 0.4, "after": 0.2, "direction": "improved"},
                "actual_variation_c_per_hour": {"before": 4, "after": 8, "direction": "worsened"}},
            "limitations": ["joint_parameter_change; individual_parameter_effects_not_identifiable"],
            "source_facts": [{"start": "2026-09-01T00:00:00+00:00", "end": "2026-09-02T00:00:00+00:00",
                              "algorithm_version": ALGORITHM_VERSION, "settings_version": "opaque-version"}],
        }]},
        "regulation_facts": {"circuits": [{
            "classification": "cycles", "coverage": {"eligible_seconds": 7200,
                                                         "exclusion_seconds": {"ventilation": 600}},
            "cycle_counts": {"eligible": 3, "partial": 1, "excluded": 1},
            "metrics": {"cycle_mean_overshoot_c": {"value": 0.8, "unit": "°C", "coverage_pct": 80}},
            "exclusion_windows": [{"reason": "ventilation", "start": "2026-09-01T10:00:00+00:00",
                                   "ended_at": "2026-09-01T11:00:00+00:00", "recovered": True}],
        }]},
    }


def test_mixed_outcome_separates_room_and_flow_and_explains_recovery() -> None:
    text = "\n".join(regulation_text(_context(), "weekly", "UTC"))
    assert "разнонаправленные изменения" in text
    assert "комнаты от цели: до 0,4, после 0,2 °C; улучшение" in text
    assert "измеренной температуры подачи: до 4, после 8 °C/ч; ухудшение" in text
    assert "Пригодно 2 ч" in text and "полных пригодных циклов запроса 3" in text
    assert "восстановление наблюдалось 01.09.2026 11:00 UTC" in text
    assert "точное время неизвестно" in text
    assert "отдельный эффект каждого не установлен" in text
    assert "экономия газа здесь не установлены" in text


def test_evidence_collapsed_and_escaped_and_input_not_changed() -> None:
    context = _context()
    context["regulation_comparisons"]["results"][0]["source_facts"][0]["settings_version"] = "<script>evil</script>"
    original = deepcopy(context)
    rendered = regulation_html(context, "daily", "UTC")
    assert context == original
    assert "<script>evil</script>" not in rendered
    assert "&lt;script&gt;evil&lt;/script&gt;" in rendered
    assert rendered.index("<details>") < rendered.index("&lt;script&gt;evil")
    assert "<details open" not in rendered
    assert "Одних суток недостаточно" in rendered


def test_missing_facts_unknown_codes_and_stale_interpretation() -> None:
    context = {"control_settings_history": {"versions": [], "unknown_before_first_observation": True},
               "regulation_interpretation_stale": True,
               "regulation_facts": {"incomplete": True, "unknowns": ["future_technical_code"]}}
    rendered = regulation_html(context, "seasonal", "UTC")
    assert "AI-интерпретация устарела" in rendered
    assert "будущ" not in rendered
    assert "future_technical_code" not in rendered
    assert "Дополнительное ограничение расчёта" in rendered
    assert "отсутствующие диапазоны не проверены" in rendered
    assert "До первого наблюдения" in rendered
    assert "Выборка неполна" in rendered


def test_no_regulation_context_does_not_change_old_report() -> None:
    assert regulation_html({}, "daily", "UTC") == ""
    assert regulation_text({}, "daily", "UTC") == []


def test_comparison_is_associated_with_its_own_circuit() -> None:
    context = _context()
    first = context["regulation_facts"]["circuits"][0]
    first["scope"] = "fixture/first"
    context["regulation_facts"]["circuits"].append({**first, "scope": "fixture/second"})
    context["regulation_comparisons"]["results"][0]["scope"] = "fixture/second"
    text = "\n".join(regulation_text(context, "weekly", "UTC"))
    assert "Контур 2: сравнение изменения настроек." in text
    assert "Контур 1: сравнение изменения настроек." not in text


def test_calculated_metrics_have_specific_labels_and_readable_durations() -> None:
    context = _context()
    context["regulation_facts"]["circuits"][0]["metrics"] = {
        "cycle_mean_heating_seconds": {"value": 3600, "unit": "seconds", "coverage_pct": 100},
        "cycle_mean_peak_delay_seconds": {"value": 1200, "unit": "seconds", "coverage_pct": 100},
        "command_total_variation_c": {"value": 320, "unit": "°C", "coverage_pct": 100},
        "cycle_mean_end_error_c": {"value": 0.45, "unit": "°C", "coverage_pct": 100},
    }
    text = "\n".join(regulation_text(context, "weekly", "UTC"))
    assert "Длительность запроса отопления в полном цикле: 1 ч" in text
    assert "Задержка пика температуры комнаты после снятия запроса: 20 мин" in text
    assert "Сумма изменений заданной температуры подачи: 320 °C" in text
    assert "Среднее отклонение комнаты от цели при снятии запроса: 0,45 °C" in text
    assert "Дополнительный рассчитанный показатель" not in text


def test_history_return_of_previous_values_and_unknown_old_value() -> None:
    def version(at: int, value: int, kind: str) -> dict:
        return {"kind": kind, "observed_at": at, "version": str(value),
                "state": {"fields": {"pid_prop_koef": {"status": "observed", "value": value}}},
                "changed_fields": ["pid_prop_koef"], "boundary": {"after": at - 3600000000,
                                                                          "no_later_than": at}}
    context = {"control_settings_history": {"versions": [version(1000000000000, 5, "first_observation"),
                                                         version(2000000000000, 3, "change"),
                                                         version(3000000000000, 5, "change")]}}
    text = "\n".join(regulation_text(context, "monthly", "UTC"))
    assert "5 → 3" in text and "3 → 5" in text
    assert "текущий снимок не распространяется на прошлые периоды" in text
    assert "наблюдавшимся версиям настроек" in text


def test_history_curve_decodes_vendor_decikelvin_and_rejects_invalid_coordinates() -> None:
    def curve_version(x: list, y: list) -> dict:
        return {"kind": "change", "changed_fields": ["pza_curve"],
                "observed_at": 1000000000000,
                "boundary": {"after": 900000000000, "no_later_than": 1000000000000},
                "state": {"fields": {"pza_curve": {"status": "observed", "x": x, "y": y}}}}
    context = {"control_settings_history": {"versions": [curve_version(
        [2430, 2530, 2630, 2730, 2830, 2930, 3030],
        [3330, 3280, 3230, 3180, 3130, 3030, 2980])]}}
    text = "\n".join(regulation_text(context, "daily", "UTC"))
    assert "на улице 20 °C → теплоноситель 30 °C" in text
    assert "на улице -30 °C → теплоноситель 60 °C" in text
    assert "2930 °C" not in text and "3030 °C" not in text
    context["control_settings_history"]["versions"] = [curve_version([20, 30], [30, 40])]
    text = "\n".join(regulation_text(context, "daily", "UTC"))
    assert "погодная таблица не расшифрована; температуры неизвестны" in text
    assert "на улице 20 °C" not in text


def test_actual_comparison_reasons_preserve_side_and_specific_cause() -> None:
    context = _context()
    context["regulation_comparisons"]["results"][0].update(
        stage="insufficient", outcome=None,
        next_observation=["before:observe_more_useful_hours", "after:actual_flow_coverage_insufficient",
                          "weather_target_or_mode_unknown_or_changing"])
    text = "\n".join(regulation_text(context, "weekly", "UTC"))
    assert "До: нужно больше пригодных часов наблюдения" in text
    assert "После: недостаточное покрытие измеренной подачи" in text
    assert "погода, цель или режим неизвестны либо меняются" in text
    assert "Контур 1: наблюдаются полные циклы запроса" in text


def test_frozen_forecast_and_retrospective_expectation_are_separate_from_outcome() -> None:
    context = _context()
    context["regulation_comparisons"]["history_start"] = "2026-07-01T00:00:00+00:00"
    context["regulation_comparisons"]["history_end"] = "2026-09-01T00:00:00+00:00"
    context["regulation_comparisons"]["results"][0]["forecast_assessments"] = [{
        "original": {"goal": "Стабильная комната", "expected_effect": "Меньше колебаний",
                     "predictions": [{"expected_effect": "Ошибка меньше 0,3 °C", "verification": "Сравнить неделю"}]},
        "assessment": "requires_explicit_measurable_prediction",
        "owner_expectation_timing": "retrospective_or_unknown",
        "prediction_timing": "unknown_or_not_before_change",
    }]
    original = deepcopy(context)
    text = "\n".join(regulation_text(context, "seasonal", "UTC"))
    assert context == original
    assert "разнонаправленные изменения" in text
    assert "Исходное ожидание: Меньше колебаний" in text
    assert "Сохранённый прогноз: Ошибка меньше 0,3 °C" in text
    assert "не считается прогнозом, зафиксированным заранее" in text
    assert "01.07.2026" in text and "последними 28 днями" not in text


def test_written_measurable_expectation_is_not_reported_as_missing() -> None:
    context = _context()
    context["regulation_comparisons"]["results"][0]["forecast_assessments"] = [{
        "original": {"expected_effect": "Отклонение уменьшится минимум на 0,1 °C за семь дней",
                     "predictions": []},
        "assessment": "requires_explicit_measurable_prediction",
        "owner_expectation_timing": "retrospective_or_unknown",
        "prediction_timing": "unknown_or_not_before_change",
    }]
    text = "\n".join(regulation_text(context, "daily", "UTC"))
    assert "автоматическая проверка не выполнена" in text
    assert "минимум на 0,1 °C за семь дней" in text
    assert "нужен явный измеримый критерий" not in text
    assert "сохранённый прогноз AI был сделан" not in text


@pytest.mark.parametrize("exact_next_time", [None, "2026-09-01T18:00:00+00:00"])
def test_superseded_limits_comparison_window_without_claiming_application_time(exact_next_time) -> None:
    context = _context()
    result = context["regulation_comparisons"]["results"][0]
    result.update(stage="superseded", outcome=None, metrics={},
                  observation_end="2026-09-01T14:00:00+00:00",
                  after={"useful_hours": 0, "complete_cycles": 0},
                  limitations=["subsequent_change_truncates_after_window"])
    # The next configuration was observed after the displayed source-fact
    # period; its uncertainty can still leave no clean after-window.
    context["control_settings_history"] = {"versions": [{
        "kind": "change", "observed_at": "2026-09-03T02:00:00+00:00", "version": "following",
        "changed_fields": [], "state": {"fields": {}},
        "boundary": {"after": "2026-09-01T14:00:00+00:00", "no_later_than": "2026-09-03T02:00:00+00:00",
                     "exact_effective_at": exact_next_time},
    }]}
    original = deepcopy(context)
    for text in ("\n".join(regulation_text(context, "daily", "UTC")), regulation_html(context, "daily", "UTC")):
        assert "сравнение ограничено следующим изменением" in text
        assert "граница следующего изменения ограничивает период после" in text
        assert "достоверное окно оценки" in text
        assert "Точный момент применения следующего изменения может быть неизвестен" in text
        assert "наблюдение прервано" not in text
        assert "следующее изменение ограничило" not in text
        if exact_next_time:
            assert "Время по записи владельца, согласованной с наблюдениями: 01.09.2026 18:00 UTC" in text
        else:
            assert "не позднее 03.09.2026 02:00 UTC; точное время неизвестно" in text
    assert context == original
