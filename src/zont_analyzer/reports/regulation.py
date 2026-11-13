"""Deterministic, read-only explanation of validated regulation facts."""
from __future__ import annotations

import html
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from zont_analyzer.analytics.settings import pza_points_c

METRICS = {
    "room_absolute_error_c": "Среднее абсолютное отклонение комнаты от цели",
    "room_signed_error_c": "Среднее отклонение комнаты от цели со знаком",
    "room_overheat_degree_hours": "Накопленное превышение температуры комнаты",
    "room_underheat_degree_hours": "Накопленный дефицит температуры комнаты",
    "room_range_c": "Размах температуры комнаты",
    "actual_range_c": "Размах измеренной температуры подачи",
    "command_range_c": "Размах заданной температуры подачи",
    "actual_variation_c_per_hour": "Изменчивость измеренной температуры подачи",
    "command_variation_c_per_hour": "Изменчивость заданной температуры подачи",
    "command_total_variation_c": "Сумма изменений заданной температуры подачи",
    "actual_total_variation_c": "Сумма изменений измеренной температуры подачи",
    "room_total_variation_c": "Сумма изменений температуры комнаты",
    "room_variation_c_per_hour": "Изменчивость температуры комнаты",
    "command_max_rate_c_per_hour": "Наибольшая наблюдавшаяся скорость изменения команды подачи",
    "actual_max_rate_c_per_hour": "Наибольшая наблюдавшаяся скорость изменения измеренной подачи",
    "room_max_rate_c_per_hour": "Наибольшая наблюдавшаяся скорость изменения температуры комнаты",
    "cycle_overshoot_c": "Выбег температуры комнаты в полном цикле запроса",
    "cycle_mean_overshoot_c": "Средний выбег температуры комнаты в полном цикле запроса",
    "cycle_mean_post_heat_rise_c": "Рост температуры комнаты после снятия запроса",
    "cycle_mean_peak_delay_seconds": "Задержка пика температуры комнаты после снятия запроса",
    "cycle_mean_heating_seconds": "Длительность запроса отопления в полном цикле",
    "cycle_mean_pause_seconds": "Пауза запроса отопления в полном цикле",
    "cycle_mean_start_error_c": "Среднее отклонение комнаты от цели при начале запроса",
    "cycle_mean_end_error_c": "Среднее отклонение комнаты от цели при снятии запроса",
    "cycle_mean_room_reaction_after_seconds": "Средняя задержка реакции комнаты после начала запроса",
    "cycle_mean_target_reached_after_seconds": "Среднее время достижения цели после начала запроса",
    "confirmed_heating_burner_starts": "Подтверждённые запуски горелки на отопление",
    "eligible_confirmed_heating_burner_starts": "Запуски горелки на пригодных участках",
    "observed_request_active_seconds": "Наблюдаемое время активного запроса отопления",
    "eligible_request_active_seconds": "Время запроса на пригодных участках",
    "command_at_known_limit_pct": "Время команды на известном пределе",
    "command_at_known_water_limit_pct": "Время команды на известном пределе теплоносителя",
}
FIELDS = {
    "pid_prop_koef": "Пропорциональная составляющая (P)",
    "pid_integral_koef": "Интегральная составляющая (I)",
    "pza": "Выбор погодной таблицы", "pza_curve": "Погодная таблица",
    "hysteresis": "Гистерезис", "heat_request": "Запрос тепла",
    "min_water_temperature": "Минимальная температура теплоносителя",
    "max_water_temperature": "Максимальная температура теплоносителя",
    "water_min_temperature": "Нижняя граница температуры теплоносителя",
    "water_max_temperature": "Верхняя граница температуры теплоносителя",
    "summer_threshold": "Порог уличной температуры перехода в летний режим",
    "off_to_start_delay": "Задержка включения", "start_to_off_delay": "Задержка выключения",
    "turn_off_delay": "Задержка отключения",
    "setting_register": "Регистр настроек", "type": "Тип контура",
    "winter_summer_switch": "Переключение зима/лето",
}
LABELS = {
    "improved": "улучшение", "worsened": "ухудшение", "mixed": "разнонаправленные изменения",
    "indistinguishable": "различие не установлено", "unavailable": "оценка недоступна",
    "detected": "изменение обнаружено", "available": "сравнение доступно",
    "insufficient": "недостаточно сопоставимых данных", "superseded": "сравнение ограничено следующим изменением",
    "transition_or_collecting": "переходный процесс или сбор данных",
    "no_heat": "нет запроса отопления", "continuous": "непрерывный запрос отопления",
    "cycles": "наблюдаются полные циклы запроса", "partial": "полные циклы не подтверждены",
    "no_complete_eligible_cycles": "нет полных пригодных циклов",
    "no_eligible_observations": "нет пригодных наблюдений",
    "applicable_historical_limit_unknown": "исторически применимый предел неизвестен",
    "historical_water_limit_unknown": "исторический предел теплоносителя неизвестен",
    "missing_signal_in_shared_conditions": "нет нужного сигнала в сопоставимых условиях",
    "no_complete_observed_cycles_in_every_shared_condition": "не во всех сопоставимых условиях есть полные циклы",
    "observe_matching_weather_target_and_mode": "нужны наблюдения при совпадающих погоде, цели и режиме",
    "observe_more_hours_balanced_across_shared_conditions": "нужно больше часов в сопоставимых условиях",
    "observe_more_complete_cycles_or_sustained_continuous_heat":
        "нужны полные циклы или длительное непрерывное отопление",
    "observe_stable_result_across_independent_intervals": "нужно подтверждение результата на независимых интервалах",
    "previous_configuration_boundary_unknown": "предыдущая граница настроек неизвестна",
    "settings_history_incomplete": "история настроек неполна",
    "change_boundary_too_wide": "время изменения известно слишком неточно",
    "room_error_coverage_insufficient": "недостаточное покрытие отклонения комнаты от цели",
    "actual_flow_coverage_insufficient": "недостаточное покрытие измеренной подачи",
    "missing_regulation_facts_or_gaps": "не все участки покрыты рассчитанными фактами",
    "matching_conditions_cover_too_little_history": "сопоставимые условия охватывают слишком мало истории",
    "observe_more_useful_hours": "нужно больше пригодных часов наблюдения",
    "uncertain_change_boundary": "участок попадает в неопределённое время изменения настроек",
    "settings_version_not_applicable": "настройки не подтверждены для этого участка",
    "ineligible_regulation_fact": "участок непригоден для оценки регулирования",
    "weather_target_or_mode_unknown_or_changing": "погода, цель или режим неизвестны либо меняются",
    "subsequent_change_truncates_after_window": "граница следующего изменения ограничивает период после",
    "previous_change_truncates_baseline": "предыдущее изменение ограничило исходный период",
    "owner_reported_subsequent_action_truncates_after_window": "последующее действие владельца ограничило наблюдение",
    "joint_parameter_change": "несколько параметров изменены вместе; отдельный эффект каждого не установлен",
    "rollback": "возврат прежних значений не подтверждает возврат прежних условий",
    "unmatched_owner_actions": "есть дополнительные действия владельца с неопределённой связью",
    "owner_action_time_unknown": "время дополнительного действия неизвестно",
    "conflicting_owner_times": "время в записях владельца противоречиво; сохранена наблюдаемая граница",
    "continuous_heat": "при непрерывном запросе выбег полного цикла недоступен",
    "ventilation": "проветривание", "rapid_cooling": "резкое охлаждение; причина не подтверждена",
    "dhw": "приоритет горячей воды", "target_or_mode_change": "изменение цели или режима",
    "unknown": "неизвестные условия", "unknown_data": "неизвестные данные",
    "unmatched_weather_target_or_mode": "погода, цель или режим не совпадают",
    "missing_facts_require_local_backfill": "не все дни рассчитаны; требуется отдельный пересчёт фактов",
    "heating_scope_unresolved": "контур отопления не определён",
    "awaiting_comparison": "проверка прогноза ожидает достаточного сравнения",
    "requires_explicit_measurable_prediction":
        "автоматическая проверка не выполнена; сохранённый критерий и срок нужно сопоставить с наблюдениями",
    "transition": "неопределённое время изменения настроек либо переход после смены цели или режима",
    "reliability": "потеря надёжной связи или питания",
    "missing_or_stale_facts": "суточные факты отсутствуют либо устарели после новых данных",
    "stale_boundary_ancestor_requires_local_backfill":
        "изменились более ранние данные о восстановлении; нужен пересчёт с начала зависимого участка",
    "bounded_source_event_context_incomplete":
        "история событий связи неполна; отсутствие незавершённого сбоя не подтверждено",
    "bounded_event_context_incomplete": "неполная история событий связи",
    "source_connection_loss_with_confirmed_service_running":
        "сообщение о потере связи с котлом противоречит наблюдаемой работе; физическая остановка не доказана",
    "source_cloud_disconnect_with_observed_boiler_service":
        "потеря связи с облаком не подтверждает остановку котла: его работа наблюдалась",
    "source_controller_off": "контроллер был выключен",
    "source_main_power_outage": "пропало основное питание",
    "source_cloud_disconnect": "потеря связи с облаком",
    "ventilation_corroborated": "проветривание подтверждается согласованным падением температуры и влажности",
    "abrupt_room_cooling": "резкое внешнее охлаждение; причина не подтверждена",
    "partial_local_day": "неполные сутки в часовом поясе отчёта",
    "data_gap": "пропуск надёжных измерений",
    "overlapping_minute_request_windows_not_reconstructable":
        "отдельные минутные окна запроса перекрываются; их точная последовательность исключена из оценки",
}


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: Any) -> list[Mapping[str, Any]]:
    return [_map(item) for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _number(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "нет данных"
    return (f"{value:.0f}" if abs(value) >= 1000 else f"{value:.3g}").replace(".", ",")


def _time(value: Any, timezone: str) -> str:
    try:
        moment = (datetime.fromtimestamp(value / 1_000_000, UTC)
                  if isinstance(value, (int, float)) else datetime.fromisoformat(value))
        if moment.tzinfo is None:
            return "время неизвестно"
        return moment.astimezone(ZoneInfo(timezone)).strftime("%d.%m.%Y %H:%M %Z")
    except (ValueError, TypeError, OverflowError, OSError, ZoneInfoNotFoundError):
        return "время неизвестно"


def _reason(value: Any) -> str:
    code = str(value).split(";", 1)[0]
    side = ""
    if code.startswith(("before:", "after:")):
        side, code = ("До: " if code.startswith("before:") else "После: "), code.split(":", 1)[1]
    if code.startswith("recovery:"):
        return "Восстановление после воздействия: " + _reason(code.split(":", 1)[1])
    if code.startswith("missing:"):
        signal = {"room": "температура комнаты", "target": "комнатная цель",
                  "request": "запрос отопления", "command": "заданная температура подачи",
                  "actual": "измеренная температура подачи", "outdoor": "уличная температура",
                  "limit": "применимый исторический предел"}.get(code.split(":", 1)[1], "нужный сигнал")
        return side + "Не определён источник: " + signal
    if ":observe_stable" in code:
        return METRICS.get(code.split(":", 1)[0], "Показатель") + ": нужно подтверждение на независимых интервалах"
    return side + LABELS.get(code, "Дополнительное ограничение расчёта; оценка по этому основанию недоступна")


def _value(value: Any) -> str:
    if isinstance(value, Mapping):
        if value.get("status") == "observed" and isinstance(value.get("x"), list):
            points = pza_points_c(value["x"], value.get("y"))
            if points is None:
                return "погодная таблица не расшифрована; температуры неизвестны"
            return "; ".join(_value(point) for point in points)
        if "outdoor_c" in value and "flow_c" in value:
            return f"на улице {_number(value['outdoor_c'])} °C → теплоноситель {_number(value['flow_c'])} °C"
        if value.get("status") != "observed":
            return {"missing": "не наблюдалось", "invalid": "некорректные данные",
                    "unassigned": "не назначено"}.get(str(value.get("status")), "неизвестно")
        return _value(value.get("value", value.get("points_c")))
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, (int, float)):
        return _number(value)
    if isinstance(value, list):
        return "; ".join(_value(item) for item in value)
    return str(value) if value is not None else "неизвестно"


def _boundary(boundary: Any, timezone: str) -> str:
    item = _map(boundary)
    if item.get("exact_effective_at") is not None:
        return "Время по записи владельца, согласованной с наблюдениями: " + _time(item["exact_effective_at"], timezone)
    return ("Изменение произошло после " + _time(item.get("after"), timezone)
            + " и не позднее " + _time(item.get("no_later_than"), timezone) + "; точное время неизвестно.")


def _metric(name: str, item: Mapping[str, Any], *, comparison: bool = False) -> str:
    label = METRICS.get(name, "Дополнительный рассчитанный показатель")
    if name == "cycle_overshoot_c" and item.get("measurement_semantics", "").startswith("observed_positive_bin"):
        label = "Превышение цели в наблюдавшемся эпизоде запроса и последующей паузе"
    if comparison:
        unit = "°C/ч" if name.endswith("per_hour") else "°C"
        return (f"{label}: до {_number(item.get('before'))}, после {_number(item.get('after'))} {unit}; "
                + _reason(item.get("direction", "unavailable")))
    unit = {"seconds": "с", "count": "", "°C/h": "°C/ч", "°C·h": "°C·ч"}.get(
        str(item.get("unit")), str(item.get("unit") or ""))
    value = item.get("value")
    if item.get("unit") == "seconds" and isinstance(value, (int, float)) and value >= 60:
        value, unit = (value / 3600, "ч") if value >= 3600 else (value / 60, "мин")
    suffix = ""
    if item.get("value") is None:
        suffix = "; " + "; ".join(_reason(reason) for reason in item.get("unavailable_reasons", []))
    return f"{label}: {_number(value)} {unit}; покрытие {_number(item.get('coverage_pct'))}%{suffix}"


def _sections(context: Mapping[str, Any], kind: str, timezone: str) -> tuple[list[str], list[str]]:
    facts = _map(context.get("regulation_facts"))
    comparisons = _map(context.get("regulation_comparisons"))
    history = _map(context.get("control_settings_history"))
    if not (facts or comparisons or history):
        return [], []
    main = ["Результат регулирования отопления"]
    evidence: list[str] = []
    if context.get("control_settings_interpretation_stale") or context.get("regulation_interpretation_stale"):
        main.append("AI-интерпретация устарела после обновления фактов. "
                    "Прежний текст и рекомендации требуют повторной проверки; "
                    "ниже показаны актуальные рассчитанные факты.")
    main.append({
        "daily": ("За сутки показаны факты и стадия наблюдения. "
                  "Одних суток недостаточно для рекомендации изменить настройку."),
        "weekly": "Недельный вывод ограничен повторяемостью наблюдений и достаточностью сопоставимой выборки.",
        "monthly": "Месячный вывод относится к наблюдавшимся версиям настроек и проверенной устойчивости результата.",
        "seasonal": ("Сезонный вывод относится только к реально наблюдавшимся погодным условиям; "
                     "отсутствующие диапазоны не проверены."),
    }.get(kind, "Вывод ограничен наблюдавшимися условиями и достаточностью данных."))
    results = _items(comparisons.get("results"))
    circuit_labels = {str(circuit.get("scope")): f"Контур {index}"
                      for index, circuit in enumerate(_items(facts.get("circuits")), 1)
                      if circuit.get("scope")}
    if not results:
        main.append("Сравнение до и после изменения пока не сформировано. Причинный эффект настройки не установлен.")
    for result in results:
        event = _map(result.get("event"))
        if str(result.get("scope")) in circuit_labels:
            main.append(circuit_labels[str(result["scope"])] + ": сравнение изменения настроек.")
        main.append("Стадия: " + _reason(result.get("stage")) + ". Результат: "
                    + (_reason(result["outcome"]) if result.get("outcome") else "пока не установлен") + ".")
        if result.get("stage") == "superseded":
            main.append("Сокращено достоверное окно оценки после текущего изменения. "
                        "Точный момент применения следующего изменения может быть неизвестен.")
        main.append(_boundary(event.get("boundary"), timezone))
        for key, change in _map(event.get("changed_values")).items():
            main.append(f"{FIELDS.get(key, 'Дополнительный параметр контроллера')}: "
                        f"{_value(_map(change).get('before'))} → {_value(_map(change).get('after'))}")
        for side, label in (("before", "До"), ("after", "После")):
            sample = _map(result.get(side))
            episode_label = ("полных наблюдаемых эпизодов запроса" if sample.get("request_episode_precision")
                             and sample.get("request_episode_precision") != "observed_state"
                             else "полных циклов запроса")
            main.append(f"{label}: пригодных часов {_number(sample.get('useful_hours'))}; "
                        f"{episode_label} {_number(sample.get('complete_cycles'))}.")
            if sample.get("request_edge_uncertainty_seconds"):
                main.append("Границы запроса известны с точностью до "
                            + _number(sample["request_edge_uncertainty_seconds"]) + " с; "
                            "наблюдаемый эпизод не подтверждает точное время переключения или пламя горелки.")
            for interval in _items(sample.get("intervals")):
                evidence.append(f"{label}, сопоставимый интервал: {_time(interval.get('start'), timezone)} — "
                                f"{_time(interval.get('end'), timezone)}; "
                                f"версия настроек {_value(interval.get('settings_version'))}")
        for name, metric in _map(result.get("metrics")).items():
            main.append(_metric(name, _map(metric), comparison=True))
        for reason in [*result.get("next_observation", []), *result.get("limitations", [])]:
            main.append(_reason(reason))
        for reason, duration in _map(result.get("exclusion_seconds")).items():
            evidence.append(f"Исключено из сравнения: {_reason(reason)} — {_number(duration)} с")
        main.append("Это наблюдаемая связь, а не доказательство причины. "
                    "Эффект отдельных параметров и экономия газа здесь не установлены.")
        for assessment in _items(result.get("forecast_assessments")):
            original = _map(assessment.get("original"))
            main.append(("Исходный прогноз: " if original.get("predictions") else "Исходное ожидание: ")
                        + _reason(assessment.get("assessment")))
            for key, label in (("goal", "Исходная цель"), ("expected_effect", "Исходное ожидание"),
                               ("observation_window", "Срок наблюдения")):
                if original.get(key):
                    evidence.append(label + ": " + str(original[key]))
            for prediction in _items(original.get("predictions")):
                evidence.append("Сохранённый прогноз: " + str(prediction.get("expected_effect", ""))
                                + "; критерий проверки: " + str(prediction.get("verification", "не задан")))
            if assessment.get("owner_expectation_timing") == "retrospective_or_unknown":
                evidence.append("Ожидание владельца записано после изменения либо время неизвестно; "
                                "оно не считается прогнозом, зафиксированным заранее.")
            if original.get("predictions") and assessment.get("prediction_timing") == "unknown_or_not_before_change":
                evidence.append("Не подтверждено, что сохранённый прогноз AI был сделан до изменения.")
        for fact in _items(result.get("source_facts")):
            evidence.append(f"Основание сравнения: {_time(fact.get('start'), timezone)} — "
                            f"{_time(fact.get('end'), timezone)}; "
                            f"версия расчёта {_value(fact.get('algorithm_version'))}; "
                            f"версия настроек {_value(fact.get('settings_version'))}")
    versions = _items(history.get("versions"))
    shown_changes = {identifier for result in results
                     if (identifier := result.get("event_id") or _map(result.get("event")).get("event_id")) is not None}
    if versions:
        main.append("Исторические наблюдения настроек: текущий снимок не распространяется на прошлые периоды.")
    previous: Mapping[str, Any] = {}
    for version in versions:
        state = _map(_map(version.get("state")).get("fields"))
        if version.get("kind") == "change" and version.get("event_id") not in shown_changes:
            main.append(_boundary(version.get("boundary"), timezone))
            for key in version.get("changed_fields", []):
                main.append(f"{FIELDS.get(key, 'Дополнительный параметр контроллера')}: "
                            f"{_value(previous.get(key))} → {_value(state.get(key))}")
        evidence.append("Настройки наблюдались " + _time(version.get("observed_at"), timezone)
                        + "; версия " + str(version.get("version", "неизвестна")))
        previous = state
    if history.get("unknown_before_first_observation"):
        main.append("До первого наблюдения применявшиеся настройки неизвестны.")
    if history.get("incomplete") or facts.get("incomplete"):
        main.append("Выборка неполна: отсутствующие участки не считаются нормальной работой.")
    if facts.get("source_binding"):
        evidence.append("Связь датчиков с контуром определена по сохранённой текущей конфигурации; "
                        "прошлое переподключение датчиков не подтверждено.")
    for index, circuit in enumerate(_items(facts.get("circuits")), 1):
        main.append(f"Контур {index}: " + _reason(circuit.get("classification")))
        coverage = _map(circuit.get("coverage"))
        seconds = coverage.get("eligible_seconds")
        hours = seconds / 3600 if isinstance(seconds, (int, float)) else None
        counts = _map(circuit.get("cycle_counts"))
        main.append(f"Пригодно {_number(hours)} ч; полных пригодных циклов запроса {_number(counts.get('eligible'))}, "
                    f"неполных {_number(counts.get('partial'))}, исключённых {_number(counts.get('excluded'))}.")
        main.append("Запрос отопления и подтверждённая работа горелки учитываются отдельно. "
                    "Температура подачи — температура теплоносителя, а не поверхности пола.")
        prominent = {"room_absolute_error_c", "actual_variation_c_per_hour", "command_variation_c_per_hour",
                     "cycle_mean_overshoot_c", "cycle_mean_post_heat_rise_c", "actual_range_c", "command_range_c"}
        for name, item in _map(circuit.get("metrics")).items():
            (main if name in prominent else evidence).append(_metric(name, _map(item)))
        for reason, excluded in _map(coverage.get("exclusion_seconds")).items():
            main.append(f"Исключено: {_reason(reason)} — {_number(excluded)} с; причины могут перекрываться.")
        for weather, duration in _map(coverage.get("weather_seconds")).items():
            weather_label = {"below_0": "ниже 0 °C", "0_to_10": "от 0 до 10 °C",
                             "above_10": "от 10 °C", "unknown": "неизвестен"}.get(str(weather), str(weather))
            main.append(f"Наблюдавшийся погодный диапазон: {weather_label}; {_number(duration)} с")
        for window in _items(circuit.get("exclusion_windows")):
            main.append(f"{_reason(window.get('reason'))}: "
                        f"{_time(window.get('started_at', window.get('start')), timezone)}; "
                        + ("восстановление наблюдалось " + _time(window.get("ended_at"), timezone)
                           if window.get("recovered") else "восстановление пока не подтверждено"))
        for cycle in _items(circuit.get("cycles")):
            evidence.append("Интервал запроса: " + _time(cycle.get("started_at"), timezone) + " — "
                            + _time(cycle.get("heating_ended_at"), timezone)
                            + "; выбег комнаты " + _number(cycle.get("overshoot_c")) + " °C")
        for reason in circuit.get("unknowns", []):
            main.append(_reason(reason))
        missing = _items(circuit.get("missing_days"))
        if missing:
            main.append(f"Суток без актуальных фактов: {len(missing)}. "
                        + "; ".join(sorted({_reason(item.get("reason")) for item in missing})))
            for item in missing[:3]:
                evidence.append("Не рассчитан участок: " + _time(item.get("start"), timezone)
                                + "; " + _reason(item.get("reason")))
    for reason in facts.get("unknowns", []):
        main.append(_reason(reason))
    if comparisons.get("events_truncated"):
        main.append("История изменений неполна; вывод относится только к явно включённым событиям.")
    if comparisons:
        main.append("Период сравнения: " + _time(comparisons.get("history_start"), timezone) + " — "
                    + _time(comparisons.get("history_end"), timezone)
                    + ". Все пригодные наблюдения внутри этого периода учитываются до отбора примеров.")
    return main, evidence


def regulation_text(context: Mapping[str, Any], kind: str, timezone: str) -> list[str]:
    main, evidence = _sections(context, kind, timezone)
    return [*main, *(["Основания вывода:", *evidence] if evidence else [])]


def regulation_html(context: Mapping[str, Any], kind: str, timezone: str) -> str:
    main, evidence = _sections(context, kind, timezone)
    if not main:
        return ""
    paragraphs = "".join(f"<p>{html.escape(line)}</p>" for line in main[1:])
    details = ("<details><summary>Основания: интервалы и версии настроек</summary>"
               + "".join(f"<p>{html.escape(line)}</p>" for line in evidence) + "</details>") if evidence else ""
    return ('<section class="regulation-result full-width"><h2>' + html.escape(main[0])
            + "</h2>" + paragraphs + details + "</section>")
