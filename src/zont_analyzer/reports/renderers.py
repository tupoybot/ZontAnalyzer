from __future__ import annotations

import html
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from zont_analyzer.domain import Report

METRIC_LABELS = {
    "mean_temperature_c": "Средняя температура помещения",
    "temperature_range_c": "Диапазон температуры помещения",
    "outdoor_mean_temperature_c": "Средняя уличная температура",
    "outdoor_temperature_range_c": "Диапазон уличной температуры",
    "heating_target_evaluation_time_pct": "Доля времени с активным контролем уставки отопления",
    "time_in_target_band_pct": "Время в диапазоне уставки отопления",
    "time_above_target_band_pct": "Время выше диапазона уставки отопления",
    "time_below_target_band_pct": "Время ниже диапазона уставки отопления",
    "mean_absolute_target_error_c": "Среднее абсолютное отклонение от уставки отопления",
    "degree_hours_above_target": "Накопленное превышение температуры комнаты над уставкой",
    "degree_hours_below_target": "Накопленный дефицит температуры комнаты относительно уставки",
    "mean_error_while_above_target_c": "Среднее превышение температуры над уставкой",
    "mean_error_while_below_target_c": "Средний дефицит температуры относительно уставки",
    "burner_starts": "Запуски горелки",
    "burner_starts_per_hour": "Запуски горелки в час",
    "burner_duty_cycle_pct": "Доля работы горелки",
    "short_cycle_share_pct": "Доля коротких циклов",
    "median_burner_cycle_minutes": "Медианная длительность цикла горелки",
    "dhw_burner_starts": "Запуски горелки на ГВС",
    "dhw_burner_starts_per_hour": "Запуски горелки на ГВС в час",
    "dhw_burner_duty_cycle_pct": "Доля работы горелки на ГВС",
    "dhw_short_cycle_share_pct": "Доля коротких циклов ГВС",
    "dhw_median_burner_cycle_minutes": "Медианная длительность цикла ГВС",
    "dhw_episode_count": "Эпизоды догрева ГВС",
    "dhw_priority_time_pct": "Доля наблюдаемого времени с приоритетом ГВС",
    "dhw_concurrent_or_ambiguous_time_pct": "Время с неоднозначными одновременными флагами ГВС/отопления",
    "dhw_target_evaluation_time_pct": "Доля времени с известной активной целью ГВС",
    "dhw_time_below_target_pct": "Время температуры ГВС ниже активной цели",
    "dhw_degree_hours_below_target": "Накопленный дефицит температуры ГВС",
    "dhw_mean_recovery_minutes": "Среднее время восстановления температуры ГВС",
    "dhw_mean_overshoot_c": "Средний перелёт температуры ГВС",
    "dhw_confirmed_heating_pause_count": "Подтверждённые паузы отопления из-за приоритета ГВС",
    "dhw_mean_confirmed_heating_pause_minutes": "Средняя длительность подтверждённой паузы отопления",
    "dhw_mean_heating_return_delay_minutes": "Средняя задержка возврата отопления после ГВС",
    "dhw_long_heating_return_count": "Долгие возвраты отопления при подтверждённом запросе",
    "dhw_residual_heat_return_count": "Возвраты отопительной активности без пламени",
    "dhw_long_hot_flow_tail_count": "Долгие горячие хвосты подачи после ГВС",
    "dhw_activity_while_disabled_count": "Сигналы активности ГВС при OFF",
    "dhw_antilegionella_cycle_count": "Вероятные циклы антилегионеллы",
    "dhw_possible_recirculation_activity_count": "Кандидаты косвенной активности рециркуляции",
    "unconfirmed_burner_pulse_count": "Отсечённые шумовые сигналы горелки",
    "boiler_uptime_seconds": "Аптайм котла",
    "zont_uptime_seconds": "Аптайм ZONT",
    "boiler_mtbf_hours": "MTBF котельного сервиса",
    "boiler_mttr_hours": "MTTR котельного сервиса",
    # Compatibility for reports persisted before the canonical MTTR rename.
    "boiler_mtbr_hours": "MTTR котельного сервиса",
}

SPACE_HEATING_METRIC_LABELS = {
    "burner_starts": "Запуски горелки на отопление",
    "burner_starts_per_hour": "Запуски горелки на отопление в час",
    "burner_duty_cycle_pct": "Доля работы горелки на отопление",
    "short_cycle_share_pct": "Доля коротких циклов отопления",
    "median_burner_cycle_minutes": "Медианная длительность цикла отопления",
}

EVENT_LABELS = {
    "temperature_above_heating_setpoint": "Температура комнаты выше диапазона уставки отопления",
    "temperature_below_heating_setpoint": "Температура комнаты ниже диапазона уставки отопления",
    "heating_mode_change": "Изменение режима отопления",
    "target_temperature_change": "Изменение уставки отопления",
    "burner_cycle_after_control_change": "Цикл горелки после изменения управления",
    "short_burner_cycle": "Короткий цикл горелки",
    "burner_cycle": "Цикл горелки",
    "automatic_summer_mode_entered": "Контур автоматически перешёл в летнее состояние",
    "automatic_summer_mode_exited": "Контур автоматически вышел из летнего состояния",
    "dhw_reheat_episode": "Эпизод догрева ГВС",
    "dhw_concurrent_or_ambiguous": "Неоднозначные одновременные флаги ГВС и отопления",
    "dhw_long_heating_return": "Долгий возврат отопления после ГВС при подтверждённом запросе",
    "dhw_activity_while_disabled": "Активность ГВС при отключённом режиме",
    "dhw_antilegionella_cycle": "Вероятный штатный цикл антилегионеллы",
    "dhw_possible_recirculation_activity": "Возможная активность рециркуляции ГВС",
    "unconfirmed_burner_pulse": "Шумовой сигнал включения горелки",
    "boiler_connection_loss": "Потеря связи с котлом",
    "main_power_outage": "Пропадание основного питания",
}

SENSOR_GROUP_LABELS = {
    "control_temperature": "Контрольная температура контура",
    "room_temperatures": "Другие жилые комнаты",
    "technical_temperatures": "Технические помещения",
    "humidity": "Влажность",
    "return_temperatures": "Обратка",
}

SENSOR_ORIGIN_LABELS = {
    "radio_sensor": "радиодатчик",
    "wired_temperature_sensor": "проводной датчик",
    "external_sensor": "внешний датчик",
    "boiler_reported_rwt": "значение rwt котла",
}

EVIDENCE_LABELS = {
    "burner_starts_per_active_request_hour": "Запуски на час активного запроса отопления",
    "burner_starts_per_observed_hour": "Запуски на час наблюдаемого сопоставимого периода",
    "burner_cycle_median_seconds": "Медиана длительности завершённого цикла",
    "burner_cycle_p90_seconds": "P90 длительности завершённого цикла",
    "burner_longest_cycle_seconds": "Самый длинный завершённый цикл",
    "burner_runtime_request_ratio": "Время горения / время запроса отопления",
    "active_request_without_flame_ratio": "Доля запроса отопления без пламени",
    "flame_modulation_mean": "Средняя модуляция при горении на отопление",
    "flame_modulation_median": "Медиана модуляции при горении на отопление",
    "flow_vs_cs_typical_c": "Типичное отклонение подачи от cs",
    "flow_vs_cs_p90_c": "P90 отклонения подачи от cs",
    "flow_vs_cs_max_positive_overshoot_c": "Максимальный положительный перелёт подачи относительно cs",
    "dhw_temperature": "Температура ГВС",
    "recirculation": "Прямой сигнал рециркуляции",
    "heating_request_pct": "Доля наблюдаемого запроса отопления",
    "flame_pct": "Доля наблюдаемого горения на отопление",
    "dhw_pct": "Доля наблюдаемого приоритета ГВС",
    "room": "Дополнительная комната",
    "control_temperature": "Контрольная температура комнаты",
    "target_temperature": "Задание температуры комнаты",
    "outdoor_temperature": "Наружная температура",
    "flow_temperature": "Фактическая температура подачи",
    "return_temperature": "Температура обратки",
    "target_flow_temperature": "Расчётное задание подачи (cs)",
    "modulation": "Модуляция горелки",
    "room_error_c": "Ошибка температуры комнаты",
    "flow_vs_cs_c": "Отклонение подачи от cs",
    "delta_t_c": "ΔT подачи и обратки",
    "weather_cadence_seconds": "Шаг обновления наружной температуры",
    "weather_plateau_transitions": "Переходы плато наружной температуры",
    "weather_jumps_over_5c": "Скачки наружной температуры свыше 5 °C",
    "weather_change_c": "Изменение наружной температуры",
}

EVIDENCE_REASONS = {
    "insufficient_boiler_state_coverage": "недостаточно наблюдений состояния котла",
    "no_qualified_active_request": "недостаточно достоверного времени активного запроса отопления",
    "no_qualified_observed_time": "недостаточно достоверного сопоставимого времени",
    "no_complete_qualified_flame_cycles": "нет полностью наблюдаемых сопоставимых циклов",
    "missing_modulation": "ряд модуляции отсутствует",
    "insufficient_modulation_coverage": "недостаточно модуляции в интервалах горения",
    "insufficient_flow_cs_coverage": "недостаточно совместных наблюдений подачи и cs при запросе отопления",
    "unknown:modulation_zero_semantics": "семантика нулевой модуляции не подтверждена профилем",
}

EVIDENCE_UNITS = {
    "seconds": "с", "celsius": "°C", "ratio": "доля", "count/hour": "запусков/ч",
    "vendor_percent": "% шкалы адаптера", "active_request_hour": "ч запроса",
    "observed_hour": "ч наблюдений", "active_request_seconds": "с запроса",
    "qualified_heating_seconds": "с сопоставимого запроса отопления",
}

EVIDENCE_EXCLUSION_LABELS = {
    "dhw": "приоритет ГВС",
    "inactive": "неактивное отопление",
    "transition": "переход режима или уставки",
    "reliability": "ненадёжная телеметрия",
    "noise": "шумовой импульс",
}


_ARCHIVE_KINDS = frozenset({"daily", "weekly", "monthly"})

# This deliberately stays in the generated document instead of a separately
# published bundle.  Archives are standalone exports and must not depend on a
# CDN or on a second publication operation for a static asset.
_ARCHIVE_NAVIGATION_SCRIPT = r"""
(() => {
  const navigation = document.querySelector("[data-archive-navigation]");
  if (!navigation) return;

  const supportedKinds = new Set(["daily", "weekly", "monthly"]);
  const datePattern = /^\d{4}-\d{2}-\d{2}$/;
  const reportKind = supportedKinds.has(navigation.dataset.reportKind) ? navigation.dataset.reportKind : "daily";
  const reportStart = navigation.dataset.reportStart || "";
  let activeKind = reportKind;
  let displayedMonth = reportStart || "";
  let reports = [];

  function archiveRoot(pathname) {
    const archived = pathname.match(/^(.*\/)(?:daily|weekly|monthly)\/[^/]+$/);
    if (archived) return archived[1] || "/";
    if (pathname.endsWith("/latest.html")) return pathname.slice(0, -"latest.html".length) || "/";
    if (pathname.endsWith("/")) return pathname;
    const slash = pathname.lastIndexOf("/");
    return slash >= 0 ? pathname.slice(0, slash + 1) : "/";
  }

  const root = archiveRoot(window.location.pathname);
  window.ZontArchive = {root};
  const controls = navigation.querySelector(".archive-controls");
  const status = navigation.querySelector(".archive-status");
  const panel = navigation.querySelector(".archive-panel");
  const previous = navigation.querySelector('[data-archive-action="previous"]');
  const latest = navigation.querySelector('[data-archive-action="latest"]');
  const next = navigation.querySelector('[data-archive-action="next"]');
  const monthLabel = navigation.querySelector(".archive-month-label");

  function validReport(value) {
    if (!value || typeof value !== "object" || !supportedKinds.has(value.kind)) return false;
    if (!datePattern.test(value.start) || !datePattern.test(value.end) || typeof value.href !== "string") return false;
    const start = localDate(value.start);
    const end = localDate(value.end);
    const datesAreReal = !Number.isNaN(start.valueOf()) && !Number.isNaN(end.valueOf())
      && start.toISOString().slice(0, 10) === value.start && end.toISOString().slice(0, 10) === value.end;
    return datesAreReal && value.start < value.end && value.href === `${value.kind}/${value.start}.html`;
  }

  function byStart(a, b) {
    return a.start.localeCompare(b.start);
  }

  function directUrl(report) {
    return new URL(report.href, window.location.origin + root).pathname;
  }

  function localDate(value) {
    return new Date(`${value}T00:00:00Z`);
  }

  function formatBoundary(value) {
    const format = new Intl.DateTimeFormat(
      "ru-RU", {year: "numeric", month: "long", day: "numeric", timeZone: "UTC"},
    );
    return format.format(localDate(value));
  }

  function formatMonth(value) {
    const format = new Intl.DateTimeFormat(
      "ru-RU", {year: "numeric", month: "long", timeZone: "UTC"},
    );
    return format.format(localDate(value));
  }

  function dateForMonth(value) {
    const latestDaily = reports.filter((item) => item.kind === "daily").at(-1);
    const base = datePattern.test(value) ? localDate(value) : localDate(latestDaily?.start || "1970-01-01");
    return {year: base.getUTCFullYear(), month: base.getUTCMonth()};
  }

  function dateString(year, month, day) {
    return `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  }

  function currentDailyIndex(days) {
    const selected = reportKind === "daily" ? reportStart : "";
    return days.findIndex((item) => item.start === selected);
  }

  function setNavigation(days) {
    const selected = currentDailyIndex(days);
    const previousReport = selected > 0 ? days[selected - 1] : null;
    const nextReport = selected >= 0 && selected < days.length - 1 ? days[selected + 1] : null;
    const latestReport = days.at(-1) || null;
    for (const [button, item] of [[previous, previousReport], [latest, latestReport], [next, nextReport]]) {
      if (!button) continue;
      button.disabled = !item;
      button.dataset.href = item ? item.href : "";
    }
  }

  function renderDaily() {
    const days = reports.filter((item) => item.kind === "daily").sort(byStart);
    setNavigation(days);
    const {year, month} = dateForMonth(displayedMonth);
    displayedMonth = dateString(year, month, 1);
    monthLabel.textContent = formatMonth(displayedMonth);
    const firstWeekday = (new Date(Date.UTC(year, month, 1)).getUTCDay() + 6) % 7;
    const count = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
    const available = new Map(days.map((item) => [item.start, item]));
    const labels = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
    const cells = labels.map((label) => `<span class="archive-weekday">${label}</span>`);
    for (let index = 0; index < firstWeekday; index += 1) {
      cells.push('<span class="archive-day archive-empty" aria-hidden="true"></span>');
    }
    for (let day = 1; day <= count; day += 1) {
      const date = dateString(year, month, day);
      const item = available.get(date);
      const selected = date === reportStart && reportKind === "daily";
      if (item) {
        const className = `archive-day available${selected ? " selected" : ""}`;
        cells.push(
          `<a class="${className}" href="${directUrl(item)}" aria-label="Отчёт за ${date}">${day}</a>`,
        );
      } else {
        cells.push(
          `<span class="archive-day unavailable" aria-label="Нет опубликованного отчёта за ${date}">${day}</span>`,
        );
      }
    }
    panel.innerHTML = [
      '<div class="archive-calendar" role="grid" aria-label="Календарь опубликованных дневных отчётов">',
      cells.join(""),
      "</div>",
    ].join("");
  }

  function renderPeriods() {
    previous.disabled = true;
    latest.disabled = true;
    next.disabled = true;
    monthLabel.textContent = activeKind === "weekly" ? "Опубликованные недели" : "Опубликованные месяцы";
    const periods = reports.filter((item) => item.kind === activeKind).sort(byStart).reverse();
    if (!periods.length) {
      panel.innerHTML = '<p class="archive-empty-message">Нет опубликованных отчётов для этого периода.</p>';
      return;
    }
    panel.innerHTML = `<ul class="archive-periods">${periods.map((item) => {
      const selected = item.start === reportStart && reportKind === activeKind ? " aria-current=\"page\"" : "";
      const boundaries = `${formatBoundary(item.start)} — ${formatBoundary(item.end)} (конец не включён)`;
      return `<li><a href="${directUrl(item)}"${selected}>${boundaries}</a></li>`;
    }).join("")}</ul>`;
  }

  function render() {
    navigation.querySelectorAll("[data-archive-kind]").forEach((button) => {
      const selected = button.dataset.archiveKind === activeKind;
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
    });
    navigation.querySelectorAll("[data-archive-month]").forEach((button) => {
      button.disabled = activeKind !== "daily";
    });
    if (activeKind === "daily") renderDaily(); else renderPeriods();
  }

  navigation.querySelectorAll("[data-archive-kind]").forEach((button) => {
    button.addEventListener("click", () => {
      activeKind = button.dataset.archiveKind || "daily";
      render();
    });
  });
  navigation.querySelector(".archive-period-tabs")?.addEventListener("keydown", (event) => {
    const tabs = Array.from(navigation.querySelectorAll("[data-archive-kind]"));
    const current = tabs.indexOf(document.activeElement);
    if (current < 0 || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const target = event.key === "Home" ? tabs[0] : event.key === "End" ? tabs.at(-1)
      : tabs[(current + (event.key === "ArrowLeft" ? -1 : 1) + tabs.length) % tabs.length];
    target?.focus();
    target?.click();
  });
  navigation.querySelectorAll("[data-archive-month]").forEach((button) => {
    button.addEventListener("click", () => {
      if (activeKind !== "daily") return;
      const {year, month} = dateForMonth(displayedMonth);
      const shifted = new Date(Date.UTC(year, month + (button.dataset.archiveMonth === "previous" ? -1 : 1), 1));
      displayedMonth = dateString(shifted.getUTCFullYear(), shifted.getUTCMonth(), 1);
      render();
    });
  });
  [previous, latest, next].forEach((button) => button?.addEventListener("click", () => {
    if (button.dataset.href) window.location.assign(directUrl({href: button.dataset.href}));
  }));

  fetch(new URL("reports.json", window.location.origin + root), {credentials: "same-origin"})
    .then((response) => response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`)))
    .then((manifest) => {
      if (!manifest || manifest.version !== 1 || !Array.isArray(manifest.reports)) {
        throw new Error("Неверный формат manifest");
      }
      reports = manifest.reports.filter(validReport);
      controls.hidden = false;
      navigation.querySelector(".archive-nojs")?.setAttribute("hidden", "");
      status.textContent = reports.length ? "" : "В архиве пока нет опубликованных отчётов.";
      if (!datePattern.test(displayedMonth)) {
        displayedMonth = reports.filter((item) => item.kind === "daily").at(-1)?.start || "1970-01-01";
      }
      render();
    })
    .catch(() => {
      status.textContent = "Архив сейчас недоступен. Откройте последний сформированный отчёт по ссылке выше.";
    });
})();
"""


def _metric_label(name: str, context: dict[str, Any] | None = None) -> str:
    offline = name in {"boiler_uptime_seconds", "zont_uptime_seconds"} and context is not None and (
        context.get("online") is False
    )
    if context and context.get("activity_scope") == "space_heating_only":
        label = SPACE_HEATING_METRIC_LABELS.get(name, METRIC_LABELS.get(name, name.replace("_", " ")))
    else:
        label = METRIC_LABELS.get(name, name.replace("_", " "))
    return f"{label} (офлайн)" if offline else label


def _event_label(kind: str) -> str:
    return EVENT_LABELS.get(kind, kind.replace("_", " "))


def _event_details(item: Any) -> str:
    if not isinstance(item, dict) or "facts" not in item:
        return json.dumps(item, ensure_ascii=False, sort_keys=True)
    facts = item.get("facts", {})
    inference = item.get("inference", {})
    observed: list[str] = []
    if isinstance(facts, dict):
        mappings = (
            ("dhw_temperature_start_c", "ГВС в начале", "°C"),
            ("dhw_target_c", "цель", "°C"),
            ("recovery_minutes", "восстановление", "мин"),
            ("heating_return_delay_minutes", "возврат отопления", "мин"),
            ("hot_flow_tail_minutes", "горячий хвост подачи", "мин"),
            ("temperature_drop_c", "снижение температуры", "°C"),
            ("flow_temperature_rise_c", "рост температуры теплоносителя", "°C"),
            ("dhw_temperature_peak_c", "пик ГВС", "°C"),
            ("reported_duration_seconds", "длительность сигнала пламени", "с"),
            ("flow_temperature_start_c", "теплоноситель в начале", "°C"),
            ("flow_temperature_peak_c", "пик теплоносителя", "°C"),
        )
        for key, label, unit in mappings:
            if facts.get(key) is not None:
                observed.append(f"{label}: {facts[key]} {unit}")
    inferred: list[str] = []
    if isinstance(inference, dict):
        if inference.get("heating_demand") is not None:
            inferred.append(f"запрос отопления: {inference['heating_demand']}")
        if inference.get("temperature_drop_pattern") is not None:
            inferred.append(f"тип снижения температуры: {inference['temperature_drop_pattern']}")
    hypothesis = item.get("hypothesis")
    chunks = ["Наблюдалось: " + "; ".join(observed or ["см. канонический JSON"])]
    if inferred:
        chunks.append("Выведено из нескольких сигналов: " + "; ".join(inferred))
    if hypothesis:
        chunks.append("Гипотеза: " + str(hypothesis))
    return " | ".join(chunks)


def _local(value: datetime, timezone: str) -> str:
    return value.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M %Z")


def _duration_dd_hh_mm(seconds: float) -> str:
    total_minutes = max(0, int(seconds) // 60)
    days, remaining = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remaining, 60)
    return f"{days:02d}:{hours:02d}:{minutes:02d}"


def _metric_display(metric: Any) -> tuple[str, str]:
    if metric.name in {"boiler_uptime_seconds", "zont_uptime_seconds"}:
        return _duration_dd_hh_mm(float(metric.value)), "дд:чч:мм"
    if metric.name in {"boiler_mtbf_hours", "boiler_mttr_hours", "boiler_mtbr_hours"}:
        value = _duration_dd_hh_mm(float(metric.value) * 3600)
        if metric.name == "boiler_mtbf_hours" and metric.context.get("lower_bound") is True:
            value = f"> {value}"
        return value, "дд:чч:мм"
    return f"{metric.value:g}", str(metric.unit)


def _sensor_identity(item: dict[str, Any]) -> str:
    name = str(item.get("display_name") or item.get("entity_id") or "неизвестный датчик")
    external_id = str(item.get("external_id") or "?")
    origin = SENSOR_ORIGIN_LABELS.get(str(item.get("origin")), str(item.get("origin") or "источник неизвестен"))
    source_type = str(item.get("source_type") or "unknown")
    confidence = float(item.get("confidence", 0.0))
    return f"{name} (ID {external_id}; {origin}; {source_type}; уверенность {confidence:.0%})"


def _sensor_context_lines(context: Any) -> list[str]:
    if not isinstance(context, dict):
        return []
    lines: list[str] = []
    control = context.get("control_temperature")
    if isinstance(control, dict):
        lines.append(f"{SENSOR_GROUP_LABELS['control_temperature']}: {_sensor_identity(control)}")
    elif context.get("control_resolution") == "unresolved":
        lines.append(f"{SENSOR_GROUP_LABELS['control_temperature']}: связь не разрешена")
    for key in ("room_temperatures", "technical_temperatures", "humidity", "return_temperatures"):
        values = context.get(key)
        if isinstance(values, list) and values:
            identities = "; ".join(_sensor_identity(item) for item in values if isinstance(item, dict))
            if identities:
                lines.append(f"{SENSOR_GROUP_LABELS[key]}: {identities}")
    return lines


def _missing_mttr_reason(report: Report) -> str | None:
    if any(metric.name in {"boiler_mttr_hours", "boiler_mtbr_hours"} for metric in report.metrics):
        return None
    reliability = report.context.get("reliability")
    boiler = reliability.get("boiler") if isinstance(reliability, dict) else None
    if not isinstance(boiler, dict):
        return None
    if boiler.get("service_failures_with_unknown_restore", 0):
        return "Момент восстановления после подтверждённых отказов попал в разрывы наблюдаемости."
    if boiler.get("confirmed_service_failures") == 0:
        return "В доступной истории нет подтверждённых отказов котельного сервиса."
    return "Недостаточно наблюдений восстановления после подтверждённых отказов."


def _evidence_number(value: Any) -> str:
    if value is None:
        return "неизвестно"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return str(value)


def _evidence_stat_text(stat: Any) -> str:
    if not isinstance(stat, Mapping):
        return _evidence_number(stat)
    parts: list[str] = []
    for key, label in (("mean", "среднее"), ("minimum", "мин"), ("maximum", "макс"),
                       ("change", "изменение"), ("changes", "изменения"), ("delta", "Δ"), ("gaps", "пробелы"),
                       ("first", "начало"), ("last", "конец"), ("slope_per_hour", "изменение/ч"),
                       ("stale_seconds", "устар.")):
        if key in stat and stat[key] is not None:
            suffix = " с" if key == "stale_seconds" else ""
            parts.append(f"{label} {_evidence_number(stat[key])}{suffix}")
    if "coverage_pct" in stat:
        parts.append(f"покрытие {_evidence_number(stat['coverage_pct'])}%")
    if "sample_count" in stat:
        parts.append(f"точек {_evidence_number(stat['sample_count'])}")
    return "; ".join(parts) or "нет значений"


def _evidence_local(value: Any, timezone: str) -> str:
    if not isinstance(value, str):
        return _evidence_number(value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(ZoneInfo(timezone))
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return value


def _temporal_evidence_text(packet: Any) -> list[str]:
    if not isinstance(packet, Mapping):
        return []
    lines = ["Временные свидетельства отопления и ГВС:"]
    period = f"{_evidence_local(packet.get('period_start'), str(packet.get('timezone') or 'UTC'))} — " \
        f"{_evidence_local(packet.get('period_end'), str(packet.get('timezone') or 'UTC'))} " \
        f"({packet.get('timezone', 'UTC')})"
    lines.append(f"- Период: {period}; профиль: {packet.get('capability_profile', 'unknown')}; "
                 f"алгоритм: {packet.get('algorithm_version', 'unknown')}")
    metrics = packet.get("metrics")
    if isinstance(metrics, list) and metrics:
        lines.append("- Показатели работы отопления:")
        for metric in metrics:
            if not isinstance(metric, Mapping):
                continue
            value = _evidence_number(metric.get("value"))
            unit = EVIDENCE_UNITS.get(str(metric.get("unit")), str(metric.get("unit") or ""))
            details = []
            if metric.get("denominator") is not None:
                denominator_unit = str(metric.get("denominator_unit") or "").strip()
                denominator_unit = EVIDENCE_UNITS.get(denominator_unit, denominator_unit)
                details.append(
                    f"знаменатель {_evidence_number(metric['denominator'])} {denominator_unit}".strip()
                )
            if metric.get("coverage_pct") is not None:
                details.append(f"покрытие {_evidence_number(metric['coverage_pct'])}%")
            if metric.get("unavailable_reason"):
                reason = str(metric["unavailable_reason"])
                details.append(f"недоступно: {EVIDENCE_REASONS.get(reason, reason)}")
            metric_name = str(metric.get("name", metric.get("id", "метрика")))
            lines.append(f"  - {EVIDENCE_LABELS.get(metric_name, metric_name)}: {value} {unit}"
                         + (f" ({'; '.join(details)})" if details else "") +
                         " [расчёт]")
    signals = packet.get("signals")
    if isinstance(signals, Mapping) and signals:
        lines.append("- Источники и качество:")
        for key, signal in signals.items():
            if isinstance(signal, Mapping):
                name = signal.get("display_name", key)
                role = signal.get("role", "unknown")
                unit = signal.get("unit", "")
                quality = signal.get("quality", signal.get("coverage_pct"))
                suffix = f"; качество {quality}%" if isinstance(quality, (int, float)) else ""
                lines.append(f"  - {name} ({EVIDENCE_LABELS.get(str(role), role)}; {unit}; ID {key}){suffix}")
    quality = packet.get("quality")
    if isinstance(quality, Mapping) and quality:
        lines.append("- Качество по источникам:")
        for key, stat in quality.items():
            source = packet.get("signals", {}).get(key, {}) if isinstance(packet.get("signals"), Mapping) else {}
            source_name = source.get("display_name", key) if isinstance(source, Mapping) else key
            lines.append(f"  - {source_name}: {_evidence_stat_text(stat)}")
    windows = packet.get("windows")
    if isinstance(windows, list) and windows:
        lines.append("- Окна:")
        timezone = str(packet.get("timezone") or "UTC")
        for window in windows:
            if not isinstance(window, Mapping):
                continue
            start = _evidence_local(window.get("started_at"), timezone)
            end = _evidence_local(window.get("ended_at"), timezone)
            lines.append(f"  - {window.get('id', 'без ID')}: {start} — {end} ({window.get('timezone', timezone)})")
            if window.get("tags"):
                lines.append("    признаки: " + ", ".join(str(item) for item in window["tags"]))
            excluded = window.get("excluded_reasons")
            if excluded:
                labels = ", ".join(EVIDENCE_EXCLUSION_LABELS.get(str(item), str(item)) for item in excluded)
                lines.append(f"    исключено: {labels}")
            for group in ("signals", "facts"):
                values = window.get(group)
                if isinstance(values, Mapping):
                    for name, stat in values.items():
                        label = EVIDENCE_LABELS.get(str(name), str(name))
                        unit = ""
                        if group == "signals" and isinstance(signals, Mapping):
                            metadata = signals.get(name, {})
                            if isinstance(metadata, Mapping):
                                label = f"{metadata.get('display_name', label)} — {label}"
                                unit = f" ({metadata.get('unit', '')})"
                        lines.append(f"    {label}{unit} — {_evidence_stat_text(stat)}")
    unknowns = packet.get("unknowns")
    if isinstance(unknowns, list) and unknowns:
        descriptions = []
        for item in unknowns:
            if str(item).startswith("missing:"):
                role = str(item).removeprefix("missing:")
                descriptions.append(f"нет данных: {EVIDENCE_LABELS.get(role, role)}")
            else:
                descriptions.append(EVIDENCE_REASONS.get(str(item), str(item)))
        lines.append("- Ограничения: " + "; ".join(descriptions))
    exclusion_windows = packet.get("exclusion_windows")
    if isinstance(exclusion_windows, list) and exclusion_windows:
        lines.append("- Точные исключённые интервалы:")
        timezone = str(packet.get("timezone") or "UTC")
        for item in exclusion_windows:
            if isinstance(item, Mapping):
                reason = EVIDENCE_EXCLUSION_LABELS.get(str(item.get("reason")), str(item.get("reason", "неизвестно")))
                lines.append(
                    f"  - {item.get('id', 'без ID')}: {_evidence_local(item.get('started_at'), timezone)} — "
                    f"{_evidence_local(item.get('ended_at'), timezone)}; {reason}; "
                    f"источник {item.get('source', 'unknown')}"
                )
    return lines


def _temporal_evidence_html(packet: Any) -> str:
    lines = _temporal_evidence_text(packet)
    if not lines:
        return ""
    body: list[str] = []
    for line in lines[1:]:
        escaped = html.escape(line)
        if line.startswith("- "):
            body.append(f"<p>{escaped}</p>")
        elif line.startswith("  - "):
            body.append(f"<p class=\"evidence-item\">{escaped}</p>")
        elif line.startswith("    "):
            body.append(f"<p class=\"evidence-detail\">{escaped}</p>")
        else:
            body.append(f"<p>{escaped}</p>")
    return '<section class="temporal-evidence"><details><summary>Временные свидетельства отопления и ГВС</summary>' \
        + "".join(body) + "</details></section>"


def render_text(report: Report) -> str:
    lines = [
        f"ZontAnalyzer — {report.kind}",
        f"ID отчёта: {report.id}",
        f"Период: {_local(report.period_start, report.timezone)} — {_local(report.period_end, report.timezone)}",
        f"AI-интерпретация: {'да' if report.ai_used else 'нет'}",
    ]
    for metric in report.metrics:
        if metric.name in {"boiler_uptime_seconds", "zont_uptime_seconds"}:
            value, unit = _metric_display(metric)
            lines.append(f"{_metric_label(metric.name, metric.context)}: {value} {unit}")
    lines.extend(
        [
            f"Качество данных: {report.quality.score:.0%} (покрытие {report.quality.coverage_pct:.1f}%)",
            report.summary,
        ]
    )
    lines.extend(_temporal_evidence_text(report.context.get("temporal_evidence")))
    current_mode = report.context.get("current_mode")
    if isinstance(current_mode, dict):
        lines.append(
            f"Текущий режим: {current_mode.get('name', current_mode.get('id'))} "
            f"({current_mode.get('intent', 'unknown')}, политика цели: {current_mode.get('target_policy', 'unknown')})"
        )
    if report.context.get("current_target_c") is not None:
        lines.append(f"Текущая целевая температура: {report.context['current_target_c']:g} °C")
    sensor_lines = _sensor_context_lines(report.context.get("sensors"))
    if sensor_lines:
        lines.append("Датчики:")
        lines.extend(f"- {line}" for line in sensor_lines)
    heating_circuit = report.context.get("heating_circuit")
    if isinstance(heating_circuit, dict):
        auto_enabled = heating_circuit.get("automatic_summer_mode_enabled")
        auto_active = heating_circuit.get("automatic_summer_mode_active")
        threshold = heating_circuit.get("summer_threshold_c")
        lines.append(
            "Автоматический летний переход контура: "
            f"{'включён' if auto_enabled else 'выключен'}"
            + (f", порог {threshold:g} °C" if isinstance(threshold, (int, float)) else "")
            + (f", текущее летнее состояние: {'активно' if auto_active else 'неактивно'}" if auto_enabled else "")
        )
        active_time_pct = 100 - float(heating_circuit.get("inactive_time_pct", 0))
        lines.append(f"Контроль отопительной уставки был активен {active_time_pct:g}% периода.")
    dhw_interaction = report.context.get("dhw_interaction")
    if isinstance(dhw_interaction, dict):
        dhw_circuit = dhw_interaction.get("dhw_circuit", {})
        dhw_quality = dhw_interaction.get("data_quality", {})
        mode = dhw_circuit.get("current_mode") if isinstance(dhw_circuit, dict) else None
        mode_name = mode.get("name") if isinstance(mode, dict) else None
        enabled = dhw_circuit.get("current_enabled") if isinstance(dhw_circuit, dict) else None
        target = dhw_circuit.get("current_target_c") if isinstance(dhw_circuit, dict) else None
        saved_target = dhw_circuit.get("configured_or_last_target_c") if isinstance(dhw_circuit, dict) else None
        score = dhw_quality.get("score") if isinstance(dhw_quality, dict) else None
        quality_text = (
            f"качество данных {float(score):.0%}." if isinstance(score, (int, float)) else "качество неизвестно."
        )
        if enabled is False:
            saved_text = (
                f" сохранённая неактивная уставка {saved_target:g} °C;"
                if isinstance(saved_target, (int, float))
                else ""
            )
            lines.append(f"ГВС: отключена выбранным режимом {mode_name or 'без названия'};{saved_text} {quality_text}")
        else:
            lines.append(
                "ГВС: "
                + (f"режим {mode_name}; " if mode_name else "режим неизвестен; ")
                + (
                    f"активная цель {target:g} °C; "
                    if isinstance(target, (int, float))
                    else "активная цель неизвестна; "
                )
                + quality_text
            )
        recirculation = dhw_interaction.get("recirculation")
        if isinstance(recirculation, dict):
            lines.append(
                "Рециркуляция ГВС: "
                + ("контур указан в конфигурации; " if recirculation.get("configured_present") else "не указана; ")
                + "прямого датчика насоса нет, возможная работа определяется только косвенно."
            )
        lines.append(
            "Доказательность эпизодов ГВС: отдельно показаны наблюдавшиеся факты, "
            "выводы из нескольких сигналов и недоказанные гидравлические гипотезы."
        )
    if report.context.get("mode_change_count") or report.context.get("target_change_count"):
        lines.append(
            f"Изменения контекста: режим — {report.context.get('mode_change_count', 0)}, "
            f"цель — {report.context.get('target_change_count', 0)}"
        )
    if report.quality.flags:
        lines.append("Флаги качества: " + ", ".join(report.quality.flags))
    if report.metrics:
        lines.append("Метрики:")
        for metric in report.metrics:
            value, unit = _metric_display(metric)
            lines.append(f"- {_metric_label(metric.name, metric.context)}: {value} {unit}")
    if report.events:
        lines.append(f"События (показано до 20 из {len(report.events)}):")
        lines.extend(
            f"- [{event.severity}] {_local(event.started_at, report.timezone)} — {_event_label(event.kind)}: "
            f"{_event_details(event.details)}"
            for event in report.events[:20]
        )
    mttr_reason = _missing_mttr_reason(report)
    if mttr_reason:
        lines.append(f"MTTR котельного сервиса: нет достоверных данных. {mttr_reason}")
    if report.recommendations:
        lines.append("Рекомендации:")
        for item in report.recommendations:
            lines.extend(
                [
                    f"- [{item.priority}] {item.title} (уверенность {item.confidence:.0%})",
                    f"  Гипотеза: {item.hypothesis}",
                    f"  Действие: {item.suggested_manual_action}",
                    f"  Ожидаемый эффект: {item.expected_effect}",
                    "  Evidence: " + ", ".join((*item.evidence_metric_ids, *item.evidence_event_ids)),
                ]
            )
            if item.risks:
                lines.append("  Риски:")
                lines.extend(f"    - {risk}" for risk in item.risks)
            if item.stop_conditions:
                lines.append("  Когда остановиться:")
                lines.extend(f"    - {condition}" for condition in item.stop_conditions)
    return "\n".join(lines)


def render_html(
    report: Report,
    recommendation_feedback: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    feedback_api_base_url: str = "/api",
    owner_data: dict[str, Any] | None = None,
    latest_report_href: str = "latest.html",
) -> str:
    title = html.escape(f"ZontAnalyzer — {report.kind}")
    from zont_analyzer.reports.owner_forms import render_owner_forms

    owner_forms = render_owner_forms(report, owner_data)
    period = html.escape(
        f"{_local(report.period_start, report.timezone)} — {_local(report.period_end, report.timezone)}"
    )
    archive_kind = report.kind if report.kind in _ARCHIVE_KINDS else "daily"
    archive_start = report.period_start.astimezone(ZoneInfo(report.timezone)).date().isoformat()
    archive_end = report.period_end.astimezone(ZoneInfo(report.timezone)).date().isoformat()
    current_mode = report.context.get("current_mode")
    mode_name = current_mode.get("name") if isinstance(current_mode, dict) else None
    mode_intent = current_mode.get("intent") if isinstance(current_mode, dict) else None
    mode_context = (
        f"<p><strong>Текущий режим:</strong> {html.escape(str(mode_name))} "
        f"({html.escape(str(mode_intent))}); <strong>цель:</strong> "
        f"{html.escape(str(report.context.get('current_target_c')))} °C</p>"
        if mode_name is not None
        else ""
    )
    heating_circuit = report.context.get("heating_circuit")
    sensor_lines = _sensor_context_lines(report.context.get("sensors"))
    sensor_context = (
        '<section class="sensors"><h2>Датчики</h2><ul>'
        + "".join(f"<li>{html.escape(line)}</li>" for line in sensor_lines)
        + "</ul></section>"
        if sensor_lines
        else ""
    )
    summer_context = ""
    if isinstance(heating_circuit, dict):
        auto_enabled = heating_circuit.get("automatic_summer_mode_enabled")
        auto_active = heating_circuit.get("automatic_summer_mode_active")
        threshold = heating_circuit.get("summer_threshold_c")
        threshold_text = f"; порог {threshold:g} °C" if isinstance(threshold, (int, float)) else ""
        active_text = f"; сейчас {'активно' if auto_active else 'неактивно'}" if auto_enabled else ""
        summer_context = (
            "<p><strong>Автоматический летний переход контура:</strong> "
            f"{'включён' if auto_enabled else 'выключен'}{threshold_text}{active_text}. "
            f"Контроль отопительной уставки активен "
            f"{100 - float(heating_circuit.get('inactive_time_pct', 0)):g}% периода.</p>"
        )
    dhw_interaction = report.context.get("dhw_interaction")
    dhw_context = ""
    if isinstance(dhw_interaction, dict):
        dhw_circuit = dhw_interaction.get("dhw_circuit", {})
        dhw_quality = dhw_interaction.get("data_quality", {})
        current_mode = dhw_circuit.get("current_mode") if isinstance(dhw_circuit, dict) else None
        current_mode_name = current_mode.get("name") if isinstance(current_mode, dict) else None
        current_enabled = dhw_circuit.get("current_enabled") if isinstance(dhw_circuit, dict) else None
        current_target = dhw_circuit.get("current_target_c") if isinstance(dhw_circuit, dict) else None
        saved_target = dhw_circuit.get("configured_or_last_target_c") if isinstance(dhw_circuit, dict) else None
        quality_score = dhw_quality.get("score") if isinstance(dhw_quality, dict) else None
        recirculation = dhw_interaction.get("recirculation", {})
        if current_enabled is False:
            target_text = "OFF"
            if isinstance(saved_target, (int, float)):
                target_text += f"; сохранённая неактивная уставка {saved_target:g} °C"
        else:
            target_text = f"{current_target:g} °C" if isinstance(current_target, (int, float)) else "неизвестна"
        quality_text = f"{float(quality_score):.0%}" if isinstance(quality_score, (int, float)) else "неизвестно"
        dhw_context = (
            '<section class="dhw"><h2>ГВС ↔ отопление</h2><p>'
            f"<strong>Режим ГВС:</strong> {html.escape(str(current_mode_name or 'неизвестен'))}; "
            f"<strong>{'состояние' if current_enabled is False else 'активная цель'}:</strong> "
            f"{html.escape(target_text)}; "
            f"<strong>качество данных ГВС:</strong> "
            f"{html.escape(quality_text)}.</p>"
            + (
                "<p><strong>Рециркуляция:</strong> контур указан в конфигурации; прямого датчика насоса нет, "
                "работа оценивается только косвенно.</p>"
                if isinstance(recirculation, dict) and recirculation.get("configured_present")
                else ""
            )
            + "<p><small>Наблюдавшиеся факты отделены от выводов из нескольких сигналов и гипотез. "
            "Без прямых сигналов клапана, насоса и расхода гидравлическая причина не считается доказанной.</small></p>"
            "</section>"
        )
    metric_rows: list[str] = []
    uptime_cards: list[str] = []
    for metric in report.metrics:
        value, unit = _metric_display(metric)
        label = _metric_label(metric.name, metric.context)
        metric_rows.append(
            f"<tr><td>{html.escape(label)}</td><td>{html.escape(value)}</td><td>{html.escape(unit)}</td></tr>"
        )
        if metric.name in {"boiler_uptime_seconds", "zont_uptime_seconds"}:
            uptime_cards.append(
                f'<article class="uptime-card"><span>{html.escape(label)}</span>'
                f"<strong>{html.escape(value)}</strong><small>{html.escape(unit)}</small></article>"
            )
    mttr_reason = _missing_mttr_reason(report)
    if mttr_reason:
        metric_rows.append(
            "<tr><td>MTTR котельного сервиса</td><td>Нет достоверных данных</td>"
            f"<td><small>{html.escape(mttr_reason)}</small></td></tr>"
        )
    metrics = "".join(metric_rows)
    uptime = f'<section class="uptime-grid">{"".join(uptime_cards)}</section>' if uptime_cards else ""
    temporal_evidence = _temporal_evidence_html(report.context.get("temporal_evidence"))
    events = "".join(
        f"<tr><td>{html.escape(_local(item.started_at, report.timezone))}</td>"
        f"<td>{html.escape(item.severity)}</td><td>{html.escape(_event_label(item.kind))}</td>"
        f"<td>{html.escape(_event_details(item.details))}</td></tr>"
        for item in report.events[:50]
    )

    def html_list(values: list[str], empty: str) -> str:
        return "<ul>" + "".join(f"<li>{html.escape(value)}</li>" for value in values) + "</ul>" if values else empty

    feedback_by_id = recommendation_feedback or {}
    status_labels = {
        "new": "Новая",
        "applied": "Выполнено",
        "rejected": "Отклонено",
        "ignored": "Без реакции",
    }
    recommendation_cards: list[str] = []
    for item in report.recommendations:
        recommendation_id = item.id or ""
        state = feedback_by_id.get(recommendation_id, {})
        status = str(state.get("status", "new"))
        if status not in status_labels:
            status = "new"
        owner_note = str(state.get("owner_note") or "")
        disabled = " disabled" if not recommendation_id else ""
        recommendation_cards.append(
            f'<article class="recommendation" data-recommendation-id="{html.escape(recommendation_id, quote=True)}">'
            f"<h3>{html.escape(item.title)}</h3>"
            f'<p><strong>ID рекомендации:</strong> <code>{html.escape(recommendation_id or "не сохранена")}</code></p>'
            f'<p><strong>Статус:</strong> <span class="feedback-status status-{html.escape(status)}" '
            f'data-status="{html.escape(status)}">{status_labels[status]}</span></p>'
            f"<p><strong>Гипотеза:</strong> {html.escape(item.hypothesis)}</p>"
            f"<p><strong>Действие:</strong> {html.escape(item.suggested_manual_action)}</p>"
            f"<p><strong>Ожидаемый эффект:</strong> {html.escape(item.expected_effect)}</p>"
            f"<p><strong>Evidence:</strong> "
            f"{html.escape(', '.join((*item.evidence_metric_ids, *item.evidence_event_ids)))}</p>"
            f"<p><small>Приоритет: {html.escape(item.priority)}; уверенность: {item.confidence:.0%}</small></p>"
            f"<p><strong>Риски:</strong></p>{html_list(item.risks, '<p>Не указаны.</p>')}"
            f"<p><strong>Когда остановиться:</strong></p>"
            f"{html_list(item.stop_conditions, '<p>Не указано.</p>')}"
            '<div class="feedback-controls">'
            '<label>Комментарий владельца'
            f'<textarea class="feedback-note" rows="3" maxlength="2000"{disabled}>'
            f"{html.escape(owner_note)}</textarea></label>"
            '<div class="feedback-actions">'
            f'<button type="button" data-feedback-status="applied"{disabled}>Выполнено</button>'
            f'<button type="button" class="reject" data-feedback-status="rejected"{disabled}>Отклонить</button>'
            "</div>"
            f'<p class="saved-note"><strong>Сохранённый комментарий:</strong> '
            f'<span>{html.escape(owner_note) if owner_note else "нет"}</span></p>'
            '<p class="feedback-message" role="status" aria-live="polite"></p>'
            "</div></article>"
        )
    recommendations = "".join(recommendation_cards)
    canonical = html.escape(json.dumps(report.model_dump(mode="json"), ensure_ascii=False))
    api_base = html.escape(feedback_api_base_url.rstrip("/"), quote=True)
    latest_href = html.escape(latest_report_href, quote=True)
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title}</title><style>
body{{font:16px system-ui;max-width:960px;margin:2rem auto;padding:0 1rem;color:#20242a}}
h1{{font-size:1.6rem}}table{{border-collapse:collapse;width:100%}}
td,th{{padding:.55rem;border-bottom:1px solid #ddd;text-align:left}}
.archive-navigation{{padding:.65rem;background:#f4f7fb;border:1px solid #d8e0ea;border-radius:.7rem;
margin:0 0 1rem}}
.archive-navigation p{{margin:0}}
.archive-controls{{display:grid;grid-template-columns:max-content max-content;gap:.45rem .65rem}}
.archive-controls[hidden]{{display:none}}
.archive-period-tabs,.archive-day-actions,.archive-month-controls{{display:flex;gap:.3rem;align-items:center;
flex-wrap:wrap}}
.archive-month-controls,.archive-panel,.archive-status{{grid-column:1/-1}}
.archive-status:empty{{display:none}}
.archive-period-tabs button,.archive-day-actions button,
.archive-month-controls button{{font:inherit;font-size:.82rem;padding:.25rem .45rem;
border:1px solid #aebdce;border-radius:.4rem;background:white;color:#1d344b;cursor:pointer}}
.archive-period-tabs button[aria-selected="true"]{{background:#235d92;border-color:#235d92;color:white}}
.archive-day-actions button:disabled{{opacity:.5;cursor:default}}
.archive-month-label{{font-size:.85rem;font-weight:600;min-width:9rem;text-align:center}}
.archive-calendar{{display:grid;grid-template-columns:repeat(7,minmax(1.5rem,1fr));gap:.15rem;max-width:18rem}}
.archive-weekday,.archive-day{{min-height:1.4rem;font-size:.82rem;display:grid;place-items:center;border-radius:.35rem;font-variant-numeric:tabular-nums}}
.archive-weekday{{font-size:.7rem;color:#536579}}
.archive-day.available{{background:#dceefc;color:#123d63;font-weight:600;text-decoration:none}}
.archive-day.available:hover,.archive-day.available:focus{{outline:2px solid #235d92;outline-offset:1px}}
.archive-day.selected{{background:#235d92;color:white}}.archive-day.unavailable{{color:#9aa5b1}}
.archive-periods{{margin:.2rem 0;padding-left:1.25rem}}.archive-periods li{{margin:.45rem 0}}
.archive-empty-message,.archive-status{{color:#536579}}
.quality{{padding:.8rem;background:#eef6ff;border-radius:.5rem}}
article{{border-left:4px solid #568;padding:0 1rem;margin:1rem 0;overflow-wrap:anywhere}}
.dhw{{padding:.8rem 1rem;background:#fff8e8;border-radius:.5rem}}
.sensors{{padding:.8rem 1rem;background:#f4f4fb;border-radius:.5rem}}
.uptime-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.8rem;margin:1rem 0}}
.uptime-card{{display:grid;gap:.25rem;border:0;background:#edf8f1;border-radius:.7rem;padding:1rem;margin:0}}
.uptime-card strong{{font-size:1.8rem;font-variant-numeric:tabular-nums}}
.uptime-card small{{color:#53635a}}
.feedback-controls{{display:grid;gap:.65rem;padding:.8rem;background:#f6f8fa;border-radius:.5rem;margin:1rem 0}}
.feedback-controls label{{display:grid;gap:.35rem;font-weight:600}}
.feedback-note{{box-sizing:border-box;width:100%;font:inherit;padding:.55rem}}
.feedback-actions{{display:flex;gap:.6rem;flex-wrap:wrap}}
.feedback-actions button{{font:inherit;padding:.5rem .9rem;border:0;border-radius:.4rem;background:#287943;color:white;
cursor:pointer}}
.feedback-actions button.reject{{background:#a33b32}}
.feedback-actions button:disabled{{opacity:.55;cursor:wait}}
.feedback-status{{display:inline-block;padding:.15rem .45rem;border-radius:1rem;background:#e9edf2}}
.status-applied{{background:#dcefe2;color:#185c2d}}.status-rejected{{background:#f7dfdc;color:#812820}}
.status-ignored{{background:#e9edf2;color:#51606f}}
.saved-note,.feedback-message{{margin:.1rem 0}}.feedback-message.error{{color:#9b251d}}
.temporal-evidence{{margin:1rem 0;padding:.8rem 1rem;background:#f5f7fa;border-radius:.5rem;overflow-wrap:anywhere}}
.temporal-evidence summary{{cursor:pointer;font-weight:600}}.evidence-item{{margin:.45rem 0 0 1rem}}
.evidence-detail{{margin:.2rem 0 0 2rem;color:#536579;font-size:.92rem}}
@media(max-width:560px){{body{{margin:1rem auto}}.archive-navigation{{padding:.6rem}}
.archive-controls{{grid-template-columns:minmax(0,1fr)}}
td,th{{padding:.4rem;font-size:.9rem;vertical-align:top}}table{{display:block;overflow-x:auto}}}}
</style></head><body data-feedback-api-base="{api_base}"><h1>{title}</h1>
<nav class="archive-navigation" data-archive-navigation data-report-kind="{archive_kind}"
data-report-start="{archive_start}" data-report-end="{archive_end}" aria-label="Архив отчётов">
<p class="archive-nojs">Для просмотра другого периода откройте
<a href="{latest_href}">последний сформированный дневной отчёт</a>.</p>
<div class="archive-controls" hidden>
<div class="archive-period-tabs" role="tablist" aria-label="Период отчёта">
<button type="button" role="tab" data-archive-kind="daily">День</button>
<button type="button" role="tab" data-archive-kind="weekly">Неделя</button>
<button type="button" role="tab" data-archive-kind="monthly">Месяц</button></div>
<div class="archive-day-actions"><button type="button" data-archive-action="previous">← Предыдущий</button>
<button type="button" data-archive-action="latest" title="Последний доступный дневной отчёт">Сегодня</button>
<button type="button" data-archive-action="next">Следующий →</button></div>
<div class="archive-month-controls">
<button type="button" data-archive-month="previous" aria-label="Предыдущий месяц">←</button>
<span class="archive-month-label" aria-live="polite"></span>
<button type="button" data-archive-month="next" aria-label="Следующий месяц">→</button></div>
<p class="archive-status" role="status" aria-live="polite"></p><div class="archive-panel"></div>
</div></nav>
<p><strong>ID:</strong> <code>{html.escape(report.id)}</code></p>
<p><strong>Период:</strong> {period}</p>
<p><strong>AI-интерпретация:</strong> {"да" if report.ai_used else "нет"}</p>
{owner_forms}
{uptime}
{mode_context}
{sensor_context}
{summer_context}
{dhw_context}
<p class="quality">Качество данных: {report.quality.score:.0%}; покрытие {report.quality.coverage_pct:.1f}%</p>
{temporal_evidence}
<p>{html.escape(report.summary)}</p><h2>Метрики</h2><table><tr><th>Метрика</th><th>Значение</th><th>Единица</th></tr>{metrics}</table>
<h2>События</h2><p>Показано до 50 из {len(report.events)}.</p>
<table><tr><th>Начало</th><th>Уровень</th><th>Тип</th><th>Детали</th></tr>{events}</table>
<h2>Рекомендации</h2>{recommendations or "<p>Нет рекомендаций.</p>"}
<details><summary>Канонический JSON</summary><pre>{canonical}</pre></details>
<script>{_ARCHIVE_NAVIGATION_SCRIPT}</script>
<script>
(() => {{
  const configuredApiBase = document.body.dataset.feedbackApiBase || "/api";
  const archiveRoot = window.ZontArchive?.root || "/";
  const apiBase = configuredApiBase === "/api" || configuredApiBase === "/za/api"
    ? `${{archiveRoot}}api`.replace(/\\/{{2,}}/g, "/")
    : configuredApiBase;
  const labels = {{applied: "Выполнено", rejected: "Отклонено", ignored: "Без реакции", new: "Новая"}};

  function applyState(card, payload) {{
    const status = payload.status || "new";
    const note = payload.owner_note || "";
    const statusNode = card.querySelector(".feedback-status");
    statusNode.textContent = labels[status] || status;
    statusNode.dataset.status = status;
    statusNode.className = `feedback-status status-${{status}}`;
    card.querySelector(".feedback-note").value = note;
    card.querySelector(".saved-note span").textContent = note || "нет";
  }}

  async function request(card, options) {{
    const id = card.dataset.recommendationId;
    const response = await fetch(`${{apiBase}}/recommendations/${{encodeURIComponent(id)}}/feedback`, {{
      ...options,
      headers: {{...(options.headers || {{}})}},
      credentials: "same-origin",
    }});
    const payload = await response.json().catch(() => ({{}}));
    if (!response.ok) throw new Error(payload.error || `Ошибка HTTP ${{response.status}}`);
    applyState(card, payload);
    return payload;
  }}

  document.querySelectorAll(".recommendation[data-recommendation-id]").forEach((card) => {{
    const id = card.dataset.recommendationId;
    if (!id) return;
    const message = card.querySelector(".feedback-message");
    card.querySelectorAll("button[data-feedback-status]").forEach((button) => {{
      button.addEventListener("click", async () => {{
        const buttons = card.querySelectorAll("button[data-feedback-status]");
        buttons.forEach((item) => item.disabled = true);
        message.className = "feedback-message";
        message.textContent = "Сохраняю…";
        try {{
          await request(card, {{
            method: "PUT",
            headers: {{"Content-Type": "application/json"}},
            body: JSON.stringify({{
              status: button.dataset.feedbackStatus,
              owner_note: card.querySelector(".feedback-note").value,
            }}),
          }});
          message.textContent = "Обратная связь сохранена.";
        }} catch (error) {{
          message.className = "feedback-message error";
          message.textContent = error instanceof Error ? error.message : "Не удалось сохранить обратную связь.";
        }} finally {{
          buttons.forEach((item) => item.disabled = false);
        }}
      }});
    }});
    request(card, {{method: "GET"}}).catch((error) => {{
      message.className = "feedback-message error";
      message.textContent = error instanceof Error ? error.message : "Не удалось обновить статус.";
    }});
  }});
}})();
</script></body></html>"""
