"""Short deterministic regulation observations for the ordinary report narrative."""
from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from zont_analyzer.domain import Report
from zont_analyzer.domain.reasoning import EvidenceReference, ObservedPattern, TimeInterval

SETTING_NAMES = {
    "pid_prop_koef": "P", "pid_integral_koef": "I", "hysteresis": "гистерезис",
    "pza": "погодная таблица", "pza_curve": "погодная кривая",
    "min_water_temperature": "нижний предел теплоносителя",
    "max_water_temperature": "верхний предел теплоносителя",
    "water_min_temperature": "нижний предел теплоносителя",
    "water_max_temperature": "верхний предел теплоносителя",
    "heat_request": "запрос тепла", "winter_summer_switch": "переключение зима/лето",
}
WAIT_REASONS = {
    "observe_matching_weather_target_and_mode": "Нужны наблюдения при сопоставимых погоде, цели и режиме.",
    "observe_more_hours_balanced_across_shared_conditions": "Нужно больше часов при сопоставимых условиях до и после.",
    "observe_more_complete_cycles_or_sustained_continuous_heat":
        "Нужны полные циклы запроса или длительное непрерывное отопление.",
    "observe_stable_result_across_independent_intervals":
        "Устойчивость результата ещё нужно проверить на независимых интервалах.",
    "change_boundary_too_wide": "Время изменения настроек известно слишком неточно для сравнения.",
    "missing_regulation_facts_or_gaps": "Часть периода не покрыта рассчитанными фактами.",
    "missing_facts_require_local_backfill": "Часть суток ещё не рассчитана.",
    "settings_history_incomplete": "История настроек неполна.",
    "subsequent_change_truncates_after_window": "Следующее изменение сократило окно оценки.",
    "actual_flow_coverage_insufficient": "Недостаточно измерений температуры подачи.",
    "room_error_coverage_insufficient": "Недостаточно измерений отклонения комнаты от цели.",
}
EXCLUSION_NAMES = {
    "ventilation_corroborated": "охлаждение с одновременным снижением влажности",
    "ventilation": "период возможного проветривания",
    "abrupt_room_cooling": "резкое охлаждение комнаты с неустановленной причиной",
    "rapid_cooling": "резкое охлаждение комнаты с неустановленной причиной",
    "dhw": "приоритет горячей воды",
}


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _number(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def _moment(value: Any) -> datetime | None:
    try:
        moment = (datetime.fromtimestamp(value / 1_000_000, UTC)
                  if isinstance(value, (int, float)) and not isinstance(value, bool)
                  else datetime.fromisoformat(value) if isinstance(value, str) else None)
    except (ValueError, OverflowError, OSError):
        return None
    return moment if moment is not None and moment.tzinfo is not None else None


def _local(value: Any, timezone: str) -> str | None:
    moment = _moment(value)
    if moment is None:
        return None
    try:
        return moment.astimezone(ZoneInfo(timezone)).strftime("%d.%m.%Y %H:%M")
    except ZoneInfoNotFoundError:
        return moment.astimezone(UTC).strftime("%d.%m.%Y %H:%M UTC")


def _interval(start: Any, end: Any, timezone: str) -> TimeInterval | None:
    left, right = _moment(start), _moment(end)
    if left is None or right is None or left > right:
        return None
    return TimeInterval(started_at=left, ended_at=right, timezone=timezone)


def _pattern(identifier: str, statement: str, interval: TimeInterval | None,
             evidence_id: Any = None) -> ObservedPattern:
    evidence = ([EvidenceReference(id=evidence_id)]
                if isinstance(evidence_id, str) and evidence_id else [])
    return ObservedPattern(id=identifier, statement=statement, interval=interval,
                           evidence=evidence, epistemic_level="derived")


def _metric(metrics: Mapping[str, Any], name: str, label: str, unit: str,
            *, show_coverage: bool = True) -> str | None:
    item = _map(metrics.get(name))
    value = _number(item.get("value"))
    if value is None:
        return None
    coverage = _number(item.get("coverage_pct"))
    suffix = f" (покрытие {coverage}%)" if show_coverage and coverage is not None else ""
    return f"{label} {value} {unit}{suffix}"


def _reason(values: Any) -> str | None:
    if not isinstance(values, list):
        return None
    for value in values:
        code = str(value).split(";", 1)[0].split(":")[-1]
        if code in WAIT_REASONS:
            return WAIT_REASONS[code]
        if code.endswith("observe_stable_result_across_independent_intervals"):
            return WAIT_REASONS["observe_stable_result_across_independent_intervals"]
    return None


def _boundary(boundary: Any, timezone: str) -> str:
    value = _map(boundary)
    exact = _local(value.get("exact_effective_at"), timezone)
    if exact:
        return f"Время по записи владельца, согласованной с наблюдениями: {exact}."
    after, until = (_local(value.get(key), timezone) for key in ("after", "no_later_than"))
    if after and until:
        return f"Изменение произошло после {after} и не позднее {until}; точное время неизвестно."
    if until:
        return f"Изменение наблюдалось не позднее {until}; точное время неизвестно."
    return "Точное время изменения неизвестно."


def _setting_change(change: Mapping[str, Any]) -> str | None:
    parts: list[str] = []
    for name, pair in _map(change.get("changed_values")).items():
        if name not in SETTING_NAMES:
            continue
        if name in {"pza", "pza_curve"}:
            parts.append(SETTING_NAMES[name])
            continue
        before, after = (_map(_map(pair).get(side)) for side in ("before", "after"))
        left, right = (_number(item.get("value")) if item.get("status") == "observed" else None
                       for item in (before, after))
        if left is not None and right is not None:
            parts.append(f"{SETTING_NAMES[name]} {left} → {right}")
        else:
            parts.append(SETTING_NAMES[name])
        if len(parts) == 3:
            break
    extra = len(_map(change.get("changed_values"))) - len(parts)
    if extra > 0:
        parts.append(f"ещё {extra} параметр(а)")
    return ", ".join(parts) if parts else None


def _comparison_sentence(result: Mapping[str, Any]) -> str:
    stage = result.get("stage")
    metrics = _map(result.get("metrics"))
    if stage == "available":
        clauses = []
        for name, label, unit in (
            ("room_absolute_error_c", "среднее абсолютное отклонение комнаты от точной цели", "°C"),
            ("actual_variation_c_per_hour", "изменчивость измеренной подачи", "°C/ч"),
            ("command_variation_c_per_hour", "изменчивость заданной подачи", "°C/ч"),
        ):
            item = _map(metrics.get(name))
            before, after = _number(item.get("before")), _number(item.get("after"))
            if before is not None and after is not None:
                clauses.append(f"{label} {before} → {after} {unit}")
        if clauses:
            outcome_code = result.get("outcome")
            outcome = {"mixed": "показатели изменились разнонаправленно", "improved": "наблюдается улучшение",
                       "worsened": "наблюдается ухудшение", "indistinguishable": "различие не установлено"}.get(
                           outcome_code if isinstance(outcome_code, str) else "", "получены следующие показатели")
            return f"В сопоставимых условиях {outcome}: {'; '.join(clauses)}. " \
                   "Связь не доказывает причину или эффект отдельного параметра."
    reason = _reason(result.get("next_observation")) or _reason(result.get("limitations"))
    if stage == "superseded":
        return reason or "Следующее изменение ограничило окно оценки; результат пока не установлен."
    return reason or "Сопоставимого результата до и после пока нет."


def _latest_change(history: Mapping[str, Any], results: list[Mapping[str, Any]], scope: str) -> tuple[
    Mapping[str, Any], Mapping[str, Any]] | None:
    candidates: list[tuple[datetime, Mapping[str, Any], Mapping[str, Any]]] = []
    for result_index, result in enumerate(results):
        if scope and result.get("scope") not in (None, scope):
            continue
        event = _map(result.get("event"))
        at = _moment(_map(event.get("boundary")).get("no_later_than"))
        if at:
            candidates.append((at, {**event, "_evidence_id": f"regulation_comparisons.results[{result_index}]"},
                               result))
    # A later history event can be absent from the bounded comparison cache.
    previous: Mapping[str, Any] = {}
    for version_index, version in enumerate(_items(history.get("versions"))):
        state = _map(_map(version.get("state")).get("fields"))
        if version.get("kind") != "change":
            previous = state
            continue
        if scope and version.get("scope") not in (None, scope):
            previous = state
            continue
        at = _moment(_map(version.get("boundary")).get("no_later_than")) or _moment(version.get("observed_at"))
        if at:
            event_id = version.get("event_id")
            matching: Mapping[str, Any] = next((candidate for candidate in results if event_id is not None
                                                and candidate.get("event_id") == event_id
                                                and (not scope or candidate.get("scope") in (None, scope))), {})
            change = {**version, "_evidence_id": f"control_settings_history.versions[{version_index}]",
                      "changed_values": {
                key: {"before": previous.get(key), "after": state.get(key)}
                for key in version.get("changed_fields", [])},
            }
            candidates.append((at, change, matching))
        previous = state
    if not candidates:
        return None
    _, selected_change, selected_result = max(candidates, key=lambda candidate: candidate[0])
    return selected_change, selected_result


def regulation_observations(report: Report, *, topics: frozenset[str] | None = None) -> list[ObservedPattern]:
    """Present current calculated evidence as bounded ordinary observations."""
    selected = topics if topics is not None else frozenset({
        "room", "actual_flow", "command_flow", "cycles", "settings", "disturbances", "limitations",
    })
    context = report.context
    facts, comparisons = _map(context.get("regulation_facts")), _map(context.get("regulation_comparisons"))
    history = _map(context.get("control_settings_history"))
    if not (facts or comparisons or history):
        return []
    results = _items(comparisons.get("results"))
    circuits = _items(facts.get("circuits"))
    period = TimeInterval(started_at=report.period_start, ended_at=report.period_end,
                          timezone=report.timezone)
    observations: list[ObservedPattern] = []
    for index, circuit in enumerate(circuits, 1):
        circuit_period = _interval(circuit.get("period_start"), circuit.get("period_end"), report.timezone) or period
        prefix = f"Контур {index}: " if len(circuits) > 1 else ""
        scope = str(circuit.get("scope") or "")
        history_scope = (f"{history.get('device_id')}/{history.get('circuit_id')}"
                         if history.get("device_id") and history.get("circuit_id") else "")
        circuit_history = history if len(circuits) == 1 or history_scope == scope else {}
        metrics = _map(circuit.get("metrics"))
        chosen = [(name, label, unit) for topic, name, label, unit in (
            ("room", "room_absolute_error_c", "среднее абсолютное отклонение комнаты от точной цели", "°C"),
            ("actual_flow", "actual_range_c", "размах измеренной температуры подачи", "°C"),
            ("command_flow", "command_range_c", "размах заданной температуры подачи", "°C"),
        ) if topic in selected]
        if "actual_flow" in selected and _number(_map(metrics.get("actual_range_c")).get("value")) is None:
            chosen.append(("actual_variation_c_per_hour", "изменчивость измеренной подачи", "°C/ч"))
        if "command_flow" in selected and _number(_map(metrics.get("command_range_c")).get("value")) is None:
            chosen.append(("command_variation_c_per_hour", "изменчивость заданной подачи", "°C/ч"))
        quantities = [value for name, label, unit in chosen
                      if (value := _metric(metrics, name, label, unit, show_coverage=False))]
        if quantities:
            coverage = [_number(_map(metrics.get(name)).get("coverage_pct"))
                        for name, _, _ in chosen if _number(_map(metrics.get(name)).get("value")) is not None]
            if len(coverage) == len(quantities) and len(set(coverage)) == 1 and coverage[0] is not None:
                suffix = f" Эти показатели рассчитаны по {coverage[0]}% указанного интервала."
            else:
                suffix = ""
                quantities = [value for name, label, unit in chosen
                              if (value := _metric(metrics, name, label, unit))]
            if circuit.get("incomplete") or circuit.get("missing_days") or facts.get("incomplete"):
                suffix += " Не все сутки рассчитаны; вывод относится к доступным участкам."
            observations.append(_pattern(f"regulation:{index}:room-flow", prefix + "На пригодных участках "
                                         + "; ".join(quantities)
                                         + "." + suffix, circuit_period, f"regulation_facts.circuits[{index - 1}]"))
        classification = circuit.get("classification")
        eligible = _map(circuit.get("cycle_counts")).get("eligible")
        overshoot = _metric(metrics, "cycle_mean_overshoot_c", "среднее наибольшее превышение "
                            "комнатой точной цели", "°C", show_coverage=False)
        has_request_observation = False
        if "cycles" in selected and classification == "cycles" and isinstance(eligible, int) and eligible > 0:
            has_request_observation = True
            minute_count = _map(circuit.get("request_observation_totals")).get("minute_observation_count")
            minute = (isinstance(minute_count, (int, float)) and minute_count > 0
                      or any(cycle.get("edge_precision") == "preceding_minute"
                             for cycle in _items(circuit.get("cycles"))))
            precision = "полных наблюдаемых эпизодов" if minute else "полных циклов"
            statement = f"За период учтено {eligible} {precision} запроса отопления"
            if overshoot:
                window = "эпизоде и следующей паузе" if minute else "полном цикле"
                statement += f"; {overshoot} в {window}"
            if minute:
                statement += "; границы минутных эпизодов приблизительны, пламя горелки не подтверждено"
            observations.append(_pattern(f"regulation:{index}:cycles", prefix + statement + ".", circuit_period,
                                         f"regulation_facts.circuits[{index - 1}]"))
        elif ("cycles" in selected and classification == "continuous"
              and _map(circuit.get("coverage")).get("eligible_seconds")):
            has_request_observation = True
            observations.append(_pattern(f"regulation:{index}:continuous",
                                         prefix + "На пригодном участке запрос отопления оставался непрерывным; "
                                         "выбег полного цикла оценить нельзя.", circuit_period,
                                         f"regulation_facts.circuits[{index - 1}]"))
        latest = _latest_change(circuit_history, results, scope) if "settings" in selected else None
        if latest:
            change, result = latest
            label = _setting_change(change) or "изменение настроек отопления"
            boundary = _map(change.get("boundary"))
            statement = f"{prefix}Изменены настройки: {label}. {_boundary(boundary, report.timezone)}"
            if result:
                statement += " " + _comparison_sentence(result)
            else:
                statement += " Для сравнения нужны наблюдения при сопоставимых погоде, цели и режиме."
            observations.append(_pattern(f"regulation:{index}:settings", statement,
                                         _interval(boundary.get("after"), boundary.get("no_later_than"),
                                                   report.timezone) or period,
                                         change.get("_evidence_id")))
        windows = [window for window in _items(circuit.get("exclusion_windows")) if "disturbances" in selected
                   if str(window.get("reason")).split(";", 1)[0] in EXCLUSION_NAMES]
        if windows:
            names = list(dict.fromkeys(EXCLUSION_NAMES[str(window.get("reason")).split(";", 1)[0]]
                                       for window in windows))
            recovered = sum(window.get("recovered") is True for window in windows)
            pending = sum(window.get("recovered") is False for window in windows)
            total = circuit.get("exclusion_windows_total")
            sampled = bool(circuit.get("exclusion_windows_truncated")) or (
                isinstance(total, int) and total > len(_items(circuit.get("exclusion_windows"))))
            statement = prefix + ("Среди исключённых участков: " if sampled else "Из оценки настроек исключены: ")
            statement += ', '.join(names)
            if len(windows) == 1 and (started := _local(
                windows[0].get("started_at", windows[0].get("start")), report.timezone,
            )):
                statement += f" ({started})"
            statement += "."
            if len(windows) == 1 and recovered:
                ended = _local(windows[0].get("ended_at"), report.timezone)
                statement += f" Восстановление наблюдалось{f' {ended}' if ended else ''}."
            elif recovered:
                statement += " Восстановление наблюдалось в показанных эпизодах."
            if pending:
                statement += " Для части эпизодов восстановление ещё не подтверждено."
            observations.append(_pattern(f"regulation:{index}:exclusions", statement, circuit_period,
                                         f"regulation_facts.circuits[{index - 1}]"))
        if "limitations" in selected and not quantities and not has_request_observation and not latest and not windows:
            reason = _reason(circuit.get("unknowns")) or (
                "Часть периода не покрыта рассчитанными фактами." if circuit.get("incomplete") else
                "Пока нет пригодных измерений для вывода о регулировании.")
            observations.append(_pattern(f"regulation:{index}:limited", prefix + reason, circuit_period,
                                         f"regulation_facts.circuits[{index - 1}]"))
    if not circuits:
        latest = _latest_change(history, results, "") if "settings" in selected else None
        if latest:
            change, result = latest
            boundary = _map(change.get("boundary"))
            statement = (f"Изменены настройки: {_setting_change(change) or 'параметры отопления'}. "
                         + _boundary(boundary, report.timezone) + " "
                         + (_comparison_sentence(result) if result else
                            "Для сравнения нужны наблюдения при сопоставимых погоде, цели и режиме."))
            observations.append(_pattern("regulation:settings", statement,
                                         _interval(boundary.get("after"), boundary.get("no_later_than"),
                                                   report.timezone) or period,
                                         change.get("_evidence_id")))
        elif "limitations" in selected and (facts.get("incomplete") or comparisons.get("history_incomplete")):
            observations.append(_pattern("regulation:limited", "Нет пригодных рассчитанных фактов для оценки "
                                         "регулирования за этот период.", period))
    return observations
