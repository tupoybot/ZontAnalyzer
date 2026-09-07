from __future__ import annotations

import html
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from zont_analyzer.domain import Report
from zont_analyzer.reports.experiment_forms import experiment_form
from zont_analyzer.reports.presentation import (
    LEGACY_DUTY_METRICS,
    _gas_value,
    burner_usage_rows,
    gas_savings_text,
    number,
    period_target,
    timezone_note,
)
from zont_analyzer.reports.wording import normalize_report_for_display

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


_ARCHIVE_KINDS = frozenset({"daily", "weekly", "monthly", "seasonal"})

# This deliberately stays in the generated document instead of a separately
# published bundle.  Archives are standalone exports and must not depend on a
# CDN or on a second publication operation for a static asset.
_ARCHIVE_NAVIGATION_SCRIPT = r"""
(() => {
  const navigation = document.querySelector("[data-archive-navigation]");
  if (!navigation) return;

  const supportedKinds = new Set(["daily", "weekly", "monthly", "seasonal"]);
  const datePattern = /^\d{4}-\d{2}-\d{2}$/;
  const reportKind = supportedKinds.has(navigation.dataset.reportKind) ? navigation.dataset.reportKind : "daily";
  const reportStart = navigation.dataset.reportStart || "";
  let activeKind = reportKind;
  let displayedMonth = reportStart || "";
  let reports = [];

  function archiveRoot(pathname) {
    const archived = pathname.match(/^(.*\/)(?:daily|weekly|monthly|seasonal)\/[^/]+$/);
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

  function setNavigation(items) {
    const previousReport = items.filter((item) => item.start < reportStart).at(-1) || null;
    const nextReport = items.find((item) => item.start > reportStart) || null;
    const latestReport = items.at(-1) || null;
    latest.textContent = activeKind === "daily" ? "Сегодня" : "Последний";
    latest.title = activeKind === "daily" ? "Последний доступный дневной отчёт"
      : "Последний доступный отчёт выбранного типа";
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
    monthLabel.textContent = activeKind === "weekly" ? "Опубликованные недели"
      : activeKind === "seasonal" ? "Опубликованные сезоны" : "Опубликованные месяцы";
    const periods = reports.filter((item) => item.kind === activeKind).sort(byStart);
    setNavigation(periods);
    if (!periods.length) {
      panel.innerHTML = '<p class="archive-empty-message">Нет опубликованных отчётов для этого периода.</p>';
      return;
    }
    panel.innerHTML = `<ul class="archive-periods">${[...periods].reverse().map((item) => {
      const selected = item.start === reportStart && reportKind === activeKind ? " aria-current=\"page\"" : "";
      const boundaries = `${formatBoundary(item.start)} — ${formatBoundary(item.end)}`;
      const partial = item.complete === false ? " · промежуточный" : "";
      const season = {spring: "Весна", summer: "Лето", autumn: "Осень", winter: "Зима"}[item.season];
      const label = `${season ? season + ": " : ""}${boundaries}${partial}`;
      return `<li><a href="${directUrl(item)}"${selected}>${label}</a></li>`;
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
      const kind = button.dataset.archiveKind || "daily";
      if (kind === activeKind) return;
      const newest = reports.filter((item) => item.kind === kind).sort(byStart).at(-1);
      if (newest) {
        window.location.assign(directUrl(newest));
        return;
      }
      status.textContent = "Нет опубликованных отчётов для выбранного типа периода.";
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
    try:
        zone = ZoneInfo(timezone)
    except Exception:
        zone = ZoneInfo("UTC")
    return value.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")


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


def _epistemic_label(level: str) -> str:
    return {"observed": "наблюдение", "derived": "расчёт по наблюдениям", "inferred": "гипотеза",
            "predicted": "прогноз"}.get(level, level)


def _known_evidence_ids(report: Report) -> set[str]:
    """Collect IDs we can verify locally, without rejecting unknown AI references."""

    known = {item.id for item in report.metrics}
    known.update(item.id for item in report.events)

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            identifier = value.get("id")
            if isinstance(identifier, str):
                known.add(identifier)
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    collect(report.context.get("temporal_evidence"))
    collect(report.context.get("heating_analysis"))
    collect(report.context.get("control_settings"))
    collect(report.context.get("gas"))
    collect(report.context.get("gas_savings"))
    dhw_profiles = report.context.get("dhw_profiles")
    if isinstance(dhw_profiles, dict):
        current = dhw_profiles.get("current")
        if isinstance(current, list):
            for episode in current:
                if isinstance(episode, dict) and isinstance(episode.get("id"), str):
                    known.add(episode["id"])
        history = dhw_profiles.get("history")
        if isinstance(history, list):
            for source in history:
                if not isinstance(source, dict) or not isinstance(source.get("episodes"), list):
                    continue
                for episode in source["episodes"]:
                    if isinstance(episode, dict) and isinstance(episode.get("id"), str):
                        known.add(episode["id"])
    noise_history = report.context.get("noise_history")
    if isinstance(noise_history, list):
        for source in noise_history:
            if not isinstance(source, dict) or not isinstance(source.get("events"), list):
                continue
            for event in source["events"]:
                if isinstance(event, dict) and isinstance(event.get("id"), str):
                    known.add(event["id"])
    return known


def _evidence_text(references: Any, known: set[str]) -> str:
    identifiers = [str(getattr(item, "id", item)) for item in references]
    if not identifiers:
        return "не указаны"
    return ", ".join(
        identifier if identifier in known else f"{identifier} (неподтверждённая ссылка)"
        for identifier in identifiers
    )


def _interval_text(interval: Any, timezone: str) -> str:
    if interval is None:
        return "интервал не указан"
    display_timezone = interval.timezone or timezone
    return (
        f"{_local(interval.started_at, display_timezone)} — "
        f"{_local(interval.ended_at, display_timezone)}"
    )


def _context_time(value: Any, timezone: str) -> str:
    if isinstance(value, datetime):
        return _local(value, timezone)
    if isinstance(value, str):
        try:
            return _local(datetime.fromisoformat(value), timezone)
        except ValueError:
            return value
    return "не указано"


def _episode_values(episode: Mapping[str, Any]) -> str:
    facts = episode.get("facts")
    inference = episode.get("inference")
    values: list[str] = []
    labels = {
        "dhw_target_c": "цель",
        "start_temperature_c": "начальная температура",
        "end_temperature_c": "конечная температура",
        "peak_temperature_c": "максимум",
        "dhw_temperature_start_c": "начальная температура",
        "dhw_temperature_end_c": "конечная температура",
        "dhw_temperature_peak_c": "максимум",
        "duration_minutes": "длительность",
        "mode": "режим",
        "selected_system_mode_name": "режим",
        "demand": "признак спроса",
    }
    for source in (facts, inference):
        if not isinstance(source, dict):
            continue
        for key, label in labels.items():
            value = source.get(key)
            if value is not None:
                values.append(f"{label}: {value}")
    return "; ".join(values) if values else "ключевые значения не сохранены"


def _historical_evidence_text(context: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    profiles = context.get("dhw_profiles")
    if isinstance(profiles, dict):
        current = profiles.get("current")
        history = profiles.get("history")
        if isinstance(current, list) or isinstance(history, list):
            lines.append("Профили эпизодов ГВС (свидетельства):")
        if isinstance(current, list):
            for episode in current:
                if isinstance(episode, dict):
                    timezone = str(episode.get("timezone") or "UTC")
                    lines.append(
                        f"- текущий [{episode.get('id', 'без ID')}]: "
                        f"{_context_time(episode.get('started_at'), timezone)} — "
                        f"{_context_time(episode.get('ended_at'), timezone)}; {_episode_values(episode)}"
                    )
        if isinstance(history, list):
            for source in history:
                if not isinstance(source, dict):
                    continue
                timezone = str(source.get("timezone") or "UTC")
                quality = source.get("quality")
                quality_text = (
                    f"качество {quality.get('score', 'не указано')}; "
                    f"покрытие {quality.get('coverage_pct', 'не указано')}%"
                    if isinstance(quality, dict)
                    else "качество не указано"
                )
                for episode in source.get("episodes", []):
                    if isinstance(episode, dict):
                        lines.append(
                            f"- история {source.get('report_id', 'без ID отчёта')} "
                            f"({_context_time(source.get('period_start'), timezone)} — "
                            f"{_context_time(source.get('period_end'), timezone)}; {quality_text}) "
                            f"[{episode.get('id', 'без ID')}]: "
                            f"{_context_time(episode.get('started_at'), timezone)} — "
                            f"{_context_time(episode.get('ended_at'), timezone)}; {_episode_values(episode)}"
                        )
    noise_history = context.get("noise_history")
    if isinstance(noise_history, list) and any(isinstance(item, dict) and item.get("events") for item in noise_history):
        lines.append("История шумовых и надёжностных событий (свидетельства):")
        for source in noise_history:
            if not isinstance(source, dict):
                continue
            for event in source.get("events", []):
                if isinstance(event, dict):
                    timezone = str(source.get("timezone") or "UTC")
                    lines.append(
                        f"- история {source.get('report_id', 'без ID отчёта')} "
                        f"[{event.get('id', 'без ID')}]: {event.get('kind', 'тип не указан')}; "
                        f"{_context_time(event.get('started_at'), timezone)} — "
                        f"{_context_time(event.get('ended_at'), timezone)}; "
                        f"покрытие {source.get('coverage_pct', 'не указано')}%"
                    )
    return lines


def _historical_evidence_html(context: Mapping[str, Any]) -> str:
    lines = _historical_evidence_text(context)
    if not lines:
        return ""
    body = "".join(f"<li>{html.escape(line[2:] if line.startswith('- ') else line)}</li>" for line in lines[1:])
    return (
        '<section class="historical-evidence"><details><summary>'
        f"{html.escape(lines[0])}</summary><ul>{body}</ul></details></section>"
    )


def render_text(report: Report) -> str:
    report = normalize_report_for_display(report)
    lines = [
        f"ZontAnalyzer — {report.kind}",
        f"ID отчёта: {report.id}",
        f"Период: {_local(report.period_start, report.timezone)} — {_local(report.period_end, report.timezone)}",
        timezone_note(report),
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
    gas = report.context.get("gas")
    if isinstance(gas, dict):
        status = str(gas.get("status") or "unknown")
        labels = {"unknown": "нет данных", "measured": "измерено", "estimated": "оценено",
                  "extrapolated": "экстраполировано"}
        lines.append(
            f"Расход газа за период: {_gas_value(gas.get('volume_m3'), 'м³')} "
            f"({labels.get(status, 'нет данных')})"
        )
        for key, label, unit in (("average_daily_m3", "Среднее за наблюдаемый день", "м³/сутки"),
                                 ("average_weekly_m3", "Среднее за наблюдаемую неделю", "м³/неделю")):
            if isinstance(gas.get(key), (int, float)):
                lines.append(f"{label}: {_gas_value(gas[key], unit)}")
        lines.extend(f"{label}: {value}" for label, value in burner_usage_rows(report))
        lines.append(f"Индекс надёжности: {_gas_value(gas.get('reliability_index_pct'), '%')}; не вероятность.")
        lines.append(f"Диапазон: {_gas_value(gas.get('lower_m3'), 'м³')} — "
                     f"{_gas_value(gas.get('upper_m3'), 'м³')}; покрытие {_gas_value(gas.get('coverage_pct'), '%')}")
        lines.append(f"Версия расчёта: {gas.get('model_version', 'неизвестна')}")
        lines.append(str(gas.get('source', '')))
        if gas.get('ai_stale'):
            lines.append("AI-интерпретация историческая и не учитывает текущую версию расчёта газа.")
    lines.extend(gas_savings_text(report))
    if report.context.get("counterfactual_question"):
        lines.append("Вопрос владельца: " + str(report.context["counterfactual_question"]))
    lines.extend(_temporal_evidence_text(report.context.get("temporal_evidence")))
    lines.extend(_historical_evidence_text(report.context))
    from zont_analyzer.reports.period_context import period_text

    lines.extend(period_text(report))
    current_mode = report.context.get("current_mode")
    if isinstance(current_mode, dict):
        lines.append(
            f"Текущий режим: {current_mode.get('name', current_mode.get('id'))} "
            f"({current_mode.get('intent', 'unknown')}, политика цели: {current_mode.get('target_policy', 'unknown')})"
        )
    target_value, target_coverage = period_target(report)
    is_period_mean = report.kind in {"weekly", "monthly", "seasonal"}
    if target_value is not None:
        target_label = "Средняя целевая температура за период" if is_period_mean else "Текущая целевая температура"
        coverage_note = f" (покрытие {target_coverage:g}% периода)" if is_period_mean else ""
        lines.append(f"{target_label}: {target_value:g} °C{coverage_note}")
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
            if metric.name in LEGACY_DUTY_METRICS:
                continue
            value, unit = _metric_display(metric)
            lines.append(f"- {_metric_label(metric.name, metric.context)}: {value} {unit}")
    if report.events:
        lines.append(f"События (показано до 20 из {len(report.events)}):")
        lines.extend(
            f"- [{event.severity}] {_local(event.started_at, report.timezone)} — "
            f"{_event_label(event.kind)}: "
            f"{_event_details(event.details)}"
            for event in report.events[:20]
        )
    mttr_reason = _missing_mttr_reason(report)
    if mttr_reason:
        lines.append(f"MTTR котельного сервиса: нет достоверных данных. {mttr_reason}")
    known_evidence = _known_evidence_ids(report)
    if report.observed_patterns:
        lines.append("Наблюдаемые паттерны:")
        for pattern in report.observed_patterns:
            lines.extend([
                f"- [{_epistemic_label(pattern.epistemic_level)}] {pattern.statement}",
                f"  Временной интервал: {_interval_text(pattern.interval, report.timezone)}",
                f"  Свидетельства: {_evidence_text(pattern.evidence, known_evidence)}",
            ])
    if report.hypotheses:
        lines.append("Гипотезы:")
        for hypothesis in report.hypotheses:
            lines.extend([
                f"- [{_epistemic_label(hypothesis.epistemic_level)}] {hypothesis.statement}",
                f"  Временной интервал: {_interval_text(hypothesis.interval, report.timezone)}",
                f"  Уверенность: {hypothesis.confidence:.0%}; "
                f"основание: {hypothesis.confidence_basis}. Это не вероятность.",
                f"  Обоснование: {hypothesis.rationale}",
                f"  Свидетельства за: {_evidence_text(hypothesis.evidence_for, known_evidence)}",
                f"  Свидетельства против: {_evidence_text(hypothesis.evidence_against, known_evidence)}",
            ])
            if hypothesis.alternatives:
                lines.append("  Альтернативы: " + "; ".join(hypothesis.alternatives))
    if report.predictions:
        lines.append("Прогнозы:")
        for prediction in report.predictions:
            lines.extend([
                f"- [{_epistemic_label(prediction.epistemic_level)}] Сценарий: {prediction.scenario}",
                f"  Ожидаемый эффект: {prediction.expected_effect}",
                f"  Уверенность: {prediction.confidence:.0%}; "
                f"основание: {prediction.confidence_basis}. Это не вероятность.",
                "  Допущения: " + ("; ".join(prediction.assumptions) if prediction.assumptions else "не указаны"),
                f"  Свидетельства: {_evidence_text(prediction.evidence, known_evidence)}",
                f"  Проверка: {prediction.verification}",
            ])
    if report.unknowns:
        lines.append("Неизвестное / недостаток данных:")
        for unknown in report.unknowns:
            lines.extend([
                f"- {unknown.statement}",
                f"  Временной интервал: {_interval_text(unknown.interval, report.timezone)}",
                f"  Свидетельства: {_evidence_text(unknown.evidence, known_evidence)}",
            ])
    if report.recommended_experiment is not None:
        experiment = report.recommended_experiment
        lines.extend([
            "Рекомендуемый ручной эксперимент:",
            f"- Переменная: {experiment.variable}; текущее значение: {experiment.current_value}; "
            f"изменение: {experiment.proposed_change}",
            f"  Обоснование: {experiment.rationale}",
            f"  Ожидаемый эффект: {experiment.expected_effect}",
            f"  Срок наблюдения: {experiment.observation_period}",
            f"  Свидетельства: {_evidence_text(experiment.evidence, known_evidence)}",
            "  Критерии успеха: "
            + ("; ".join(experiment.success_criteria) if experiment.success_criteria else "не указаны"),
            "  Когда остановиться: "
            + ("; ".join(experiment.stop_conditions) if experiment.stop_conditions else "не указано"),
            "  Риски: " + ("; ".join(experiment.risks) if experiment.risks else "не указаны"),
        ])
    if report.recommendations:
        lines.append("Рекомендации:")
        for recommendation in report.recommendations:
            lines.extend(
                [
                    f"- [{recommendation.priority}] {recommendation.title} "
                    f"(уверенность {recommendation.confidence:.0%})",
                    f"  Гипотеза: {recommendation.hypothesis}",
                    f"  Действие: {recommendation.suggested_manual_action}",
                    f"  Ожидаемый эффект: {recommendation.expected_effect}",
                    "  Свидетельства: " + _evidence_text(
                        (*recommendation.evidence_metric_ids, *recommendation.evidence_event_ids), known_evidence
                    ),
                ]
            )
            if recommendation.risks:
                lines.append("  Риски:")
                lines.extend(f"    - {risk}" for risk in recommendation.risks)
            if recommendation.stop_conditions:
                lines.append("  Когда остановиться:")
                lines.extend(f"    - {condition}" for condition in recommendation.stop_conditions)
    return "\n".join(lines)


def render_html(
    report: Report,
    recommendation_feedback: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    feedback_api_base_url: str = "/api",
    owner_data: dict[str, Any] | None = None,
    latest_report_href: str = "latest.html",
    chart_data: dict[str, Any] | None = None,
) -> str:
    from . import presentation as ui
    from .charts import render_charts
    from .theme import SCRIPT, STYLE

    canonical_report = report
    report = normalize_report_for_display(report)
    title = html.escape(f"ZontAnalyzer — {report.kind}")
    from zont_analyzer.reports.owner_forms import render_owner_forms

    owner_forms = render_owner_forms(report, owner_data)
    from zont_analyzer.reports.period_context import render_period_context
    from zont_analyzer.reports.regeneration import render_regeneration

    regeneration = render_regeneration(report, feedback_api_base_url)
    period_context = render_period_context(report)
    period = html.escape(
        f"{_local(report.period_start, report.timezone)} — {_local(report.period_end, report.timezone)}"
    )
    archive_kind = report.kind if report.kind in _ARCHIVE_KINDS else "daily"
    archive_start = report.period_start.astimezone(ZoneInfo(report.timezone)).date().isoformat()
    archive_end = report.period_end.astimezone(ZoneInfo(report.timezone)).date().isoformat()
    current_mode = report.context.get("current_mode")
    mode_name = current_mode.get("name") if isinstance(current_mode, dict) else None
    mode_intent = current_mode.get("intent") if isinstance(current_mode, dict) else None
    target_value, target_coverage = period_target(report)
    is_period_mean = report.kind in {"weekly", "monthly", "seasonal"}
    target_label = "Средняя цель за период" if is_period_mean else "Цель на конец периода"
    mode_context = (
        f"<p><strong>Режим на конец периода:</strong> {html.escape(str(mode_name))} "
        f'<span class="debug-only">{html.escape(str(mode_intent))}</span></p>'
        f'<p><strong>{target_label}:</strong> {html.escape(number(target_value, "°C"))}</p>'
        if mode_name is not None
        else ""
    )
    heating_circuit = report.context.get("heating_circuit")
    sensor_lines = _sensor_context_lines(report.context.get("sensors"))
    sensor_context = (
        '<section class="sensors debug-only"><h2>Источники датчиков</h2><ul>'
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
            f"{ui.number(100 - float(heating_circuit.get('inactive_time_pct', 0)), '%')} периода.</p>"
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
    mttr_reason = _missing_mttr_reason(report)
    temporal_evidence = _temporal_evidence_html(report.context.get("temporal_evidence"))
    historical_evidence = _historical_evidence_html(report.context)
    question = report.context.get("counterfactual_question")
    question_html = (
        "<p><strong>Вопрос владельца:</strong> " + html.escape(str(question)) + "</p>" if question else ""
    )
    gas_context = report.context.get("gas")
    if isinstance(gas_context, dict) and gas_context.get("ai_stale") is True:
        question_html = ('<p class="gas-period-stale" role="status"><strong>AI-интерпретация историческая:</strong> '
                         'расчёт расхода газа обновлён после этого AI-ответа.</p>' + question_html)

    def html_list(values: list[str], empty: str) -> str:
        return "<ul>" + "".join(f"<li>{html.escape(value)}</li>" for value in values) + "</ul>" if values else empty

    known_evidence = _known_evidence_ids(report)

    def evidence_html(references: Any) -> str:
        return html.escape(_evidence_text(references, known_evidence))

    reasoning_parts: list[str] = []
    if report.observed_patterns:
        cards = "".join(
            '<article class="reasoning-item observed">'
            f"<h3>{html.escape(item.statement)}</h3>"
            f"<p><strong>Статус:</strong> {html.escape(_epistemic_label(item.epistemic_level))}; "
            f"<strong>Интервал:</strong> {html.escape(_interval_text(item.interval, report.timezone))}</p>"
            f"<p class=\"debug-only\"><strong>Свидетельства:</strong> {evidence_html(item.evidence)}</p></article>"
            for item in report.observed_patterns
        )
        reasoning_parts.append(f"<section><h2>Наблюдаемые паттерны</h2>{cards}</section>")
    if report.hypotheses:
        cards = "".join(
            '<article class="reasoning-item hypothesis">'
            f"<h3>{html.escape(item.statement)}</h3>"
            f"<p><strong>Статус:</strong> {html.escape(_epistemic_label(item.epistemic_level))}; "
            f"<strong>Интервал:</strong> {html.escape(_interval_text(item.interval, report.timezone))}</p>"
            f"<p><strong>Уверенность:</strong> {item.confidence:.0%}; "
            f"основание: {html.escape(item.confidence_basis)}. <small>Это не вероятность.</small></p>"
            f"<p><strong>Обоснование:</strong> {html.escape(item.rationale)}</p>"
            f"<p class=\"debug-only\"><strong>Свидетельства за:</strong> {evidence_html(item.evidence_for)}</p>"
            f"<p class=\"debug-only\"><strong>Свидетельства против:</strong> {evidence_html(item.evidence_against)}</p>"
            f"<p><strong>Альтернативы:</strong></p>{html_list(item.alternatives, '<p>Не указаны.</p>')}"
            "</article>"
            for item in report.hypotheses
        )
        reasoning_parts.append(f"<section><h2>Гипотезы</h2>{cards}</section>")
    if report.predictions:
        cards = "".join(
            '<article class="reasoning-item prediction">'
            f"<h3>Сценарий: {html.escape(item.scenario)}</h3>"
            f"<p><strong>Ожидаемый эффект:</strong> {html.escape(item.expected_effect)}</p>"
            f"<p><strong>Уверенность:</strong> {item.confidence:.0%}; "
            f"основание: {html.escape(item.confidence_basis)}. <small>Это не вероятность.</small></p>"
            f"<p><strong>Допущения:</strong></p>{html_list(item.assumptions, '<p>Не указаны.</p>')}"
            f"<p class=\"debug-only\"><strong>Свидетельства:</strong> {evidence_html(item.evidence)}</p>"
            f"<p><strong>План проверки:</strong> {html.escape(item.verification)}</p>"
            "</article>"
            for item in report.predictions
        )
        reasoning_parts.append(f"<section><h2>Прогнозы</h2>{cards}</section>")
    if report.unknowns:
        cards = "".join(
            '<article class="reasoning-item unknown">'
            f"<h3>{html.escape(item.statement)}</h3>"
            f"<p><strong>Интервал:</strong> {html.escape(_interval_text(item.interval, report.timezone))}</p>"
            f"<p class=\"debug-only\"><strong>Свидетельства:</strong> {evidence_html(item.evidence)}</p></article>"
            for item in report.unknowns
        )
        reasoning_parts.append(f"<section><h2>Неизвестное / недостаток данных</h2>{cards}</section>")
    if report.recommended_experiment is not None:
        experiment = report.recommended_experiment
        reasoning_parts.append(
            '<section><h2>Рекомендуемый ручной эксперимент</h2><article class="reasoning-item experiment">'
            f"<p><strong>Переменная:</strong> {html.escape(experiment.variable)}</p>"
            f"<p><strong>Текущее значение:</strong> {html.escape(experiment.current_value)}</p>"
            f"<p><strong>Изменение:</strong> {html.escape(experiment.proposed_change)}</p>"
            f"<p><strong>Обоснование:</strong> {html.escape(experiment.rationale)}</p>"
            f"<p><strong>Ожидаемый эффект:</strong> {html.escape(experiment.expected_effect)}</p>"
            f"<p><strong>Срок наблюдения:</strong> {html.escape(experiment.observation_period)}</p>"
            f"<p class=\"debug-only\"><strong>Свидетельства:</strong> {evidence_html(experiment.evidence)}</p>"
            f"<p><strong>Критерии успеха:</strong></p>{html_list(experiment.success_criteria, '<p>Не указаны.</p>')}"
            f"<p><strong>Когда остановиться:</strong></p>{html_list(experiment.stop_conditions, '<p>Не указано.</p>')}"
            f"<p><strong>Риски:</strong></p>{html_list(experiment.risks, '<p>Не указаны.</p>')}"
            "</article></section>"
        )
    reasoning = "".join(reasoning_parts)
    # Keep each explanation accessible with its technical evidence in context.
    for heading in ("Наблюдаемые паттерны", "Гипотезы", "Прогнозы"):
        prefix = f"<section><h2>{heading}</h2>"
        start = reasoning.find(prefix)
        if start >= 0:
            end = reasoning.index("</section>", start)
            body = reasoning[start + len(prefix):end]
            reasoning = (reasoning[:start] + f"<details><summary>{heading}</summary>{body}</details>"
                         + reasoning[end + len("</section>"):])

    feedback_by_id = recommendation_feedback or {}
    status_labels = {
        "new": "Новая",
        "applied": "Выполнено",
        "rejected": "Отклонено",
        "ignored": "Без реакции",
    }
    category_labels = {
        "observe_only": "Наблюдение", "safe_user_setting": "Настройка",
        "needs_manual_context": "Нужен контекст", "service_required": "Обслуживание",
        "safety_warning": "Безопасность",
    }
    priority_labels = {"low": "низкий", "medium": "средний", "high": "высокий", "critical": "критический"}
    recommendation_cards: list[str] = []
    for recommendation in report.recommendations:
        recommendation_id = recommendation.id or ""
        state = feedback_by_id.get(recommendation_id, {})
        status = str(state.get("status", "new"))
        if status not in status_labels:
            status = "new"
        owner_note = str(state.get("owner_note") or "")
        disabled = " disabled" if not recommendation_id else ""
        note_save_hidden = " hidden" if status not in {"applied", "rejected"} else ""
        recommendation_cards.append(
            f'<article class="recommendation" data-recommendation-id="{html.escape(recommendation_id, quote=True)}">'
            f"<h3>{html.escape(recommendation.title)}</h3>"
            f'<p class="debug-only"><strong>ID рекомендации:</strong> '
            f'<code>{html.escape(recommendation_id or "не сохранена")}</code></p>'
            f'<p><strong>Статус:</strong> <span class="feedback-status status-{html.escape(status)}" '
            f'data-status="{html.escape(status)}">{status_labels[status]}</span></p>'
            f"<p><strong>Почему:</strong> {html.escape(recommendation.hypothesis)}</p>"
            f"<p><strong>Что сделать:</strong> {html.escape(recommendation.suggested_manual_action)}</p>"
            f"<p><strong>Ожидаемый эффект:</strong> {html.escape(recommendation.expected_effect)}</p>"
            f"<p class=\"debug-only\"><strong>Evidence:</strong> "
            f"{evidence_html((*recommendation.evidence_metric_ids, *recommendation.evidence_event_ids))}</p>"
            f"<p><small>{html.escape(category_labels[recommendation.category])} · "
            f"приоритет: {html.escape(priority_labels[recommendation.priority])}; "
            f"уверенность: {recommendation.confidence:.0%}</small></p>"
            '<details><summary>Риски и условия остановки</summary>'
            f"<p><strong>Риски:</strong></p>{html_list(recommendation.risks, '<p>Не указаны.</p>')}"
            f"<p><strong>Когда остановиться:</strong></p>"
            f"{html_list(recommendation.stop_conditions, '<p>Не указано.</p>')}</details>"
            '<div class="feedback-controls">'
            f'{experiment_form(state.get("experiment"), disabled, report.timezone)}'
            '<details class="feedback-comment"><summary>Комментарий… / Изменить</summary><label>Комментарий владельца'
            f'<textarea class="feedback-note" rows="3" maxlength="2000"{disabled}>'
            f"{html.escape(owner_note)}</textarea></label>"
            f'<button type="button" data-feedback-save-note{disabled}{note_save_hidden}>'
            'Сохранить комментарий</button><small>Комментарий сохраняется вместе с решением.</small></details>'
            '<div class="feedback-actions">'
            f'<button type="button" data-feedback-status="applied"{disabled}>Выполнено</button>'
            f'<button type="button" class="reject" data-feedback-status="rejected"{disabled}>Отклонить</button>'
            "</div>"
            f'<p class="saved-note"><strong>Сохранённый комментарий:</strong> '
            f'<span>{html.escape(owner_note) if owner_note else "нет"}</span></p>'
            '<p class="feedback-message" role="status" aria-live="polite"></p>'
            "</div></article>"
        )
    recommendations = "".join(recommendation_cards[:1])
    other_recommendations = "".join(recommendation_cards[1:])
    by_name = {m.name: m.value for m in report.metrics}
    pauses = by_name.get("dhw_confirmed_heating_pause_count")
    pause_minutes = by_name.get("dhw_mean_confirmed_heating_pause_minutes")
    thermal_interaction = '<p class="interaction">ГВС ↔ отопление: '
    if pauses is not None:
        thermal_interaction += f"подтверждённых пауз отопления при догреве ГВС — {int(pauses)}. "
        if pauses and pause_minutes is not None:
            thermal_interaction += f"Средняя пауза — {ui.number(pause_minutes, 'мин')}. "
        returns = by_name.get("dhw_long_heating_return_count")
        if returns == 0:
            thermal_interaction += "Долгого возврата отопления после ГВС не наблюдалось."
    else:
        thermal_interaction += "недостаточно данных для оценки пауз отопления."
    thermal_interaction += '</p>'
    more_actions = (
        '<section class="full-width more-actions"><h2>Другие рекомендации</h2>'
        f'<div class="lower-grid">{other_recommendations}</div></section>'
        if other_recommendations else ''
    )
    canonical = html.escape(json.dumps(canonical_report.model_dump(mode="json"), ensure_ascii=False))
    api_base = html.escape(feedback_api_base_url.rstrip("/"), quote=True)
    latest_href = html.escape(latest_report_href, quote=True)
    period_date = report.period_start.astimezone(ZoneInfo(report.timezone)).strftime('%d.%m.%Y')
    kind_label = {'daily': 'День', 'weekly': 'Неделя', 'monthly': 'Месяц',
                  'initial': 'Обзор', 'seasonal': 'Сезон'}[report.kind]
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title}</title><style>{STYLE}</style></head><body data-feedback-api-base="{api_base}">
<a class="skip-link" href="#report">К отчёту</a>
<header><div class="header-line"><span class="brand">ZontAnalyzer<span class="secondary"> / отчёт</span></span>
<span class="period-label">{period_date} · {kind_label}</span>
<div class="header-tools"><label>Debug <input id="debug-toggle" type="checkbox"></label>
<button type="button" data-open-profile aria-label="Открыть профиль системы">⚙ Профиль системы</button></div></div>
<nav class="archive-navigation" data-archive-navigation data-report-kind="{archive_kind}"
data-report-start="{archive_start}" data-report-end="{archive_end}" aria-label="Архив отчётов">
<p class="archive-nojs">Для просмотра другого периода откройте
<a href="{latest_href}">последний сформированный дневной отчёт</a>.</p>
<div class="archive-controls" hidden>
<div class="archive-period-tabs" role="tablist" aria-label="Период отчёта">
<button type="button" role="tab" data-archive-kind="daily">День</button>
<button type="button" role="tab" data-archive-kind="weekly">Неделя</button>
<button type="button" role="tab" data-archive-kind="monthly">Месяц</button>
<button type="button" role="tab" data-archive-kind="seasonal">Сезон</button></div>
<div class="archive-day-actions"><button type="button" data-archive-action="previous">← Предыдущий</button>
<button type="button" data-archive-action="latest" title="Последний доступный дневной отчёт">Сегодня</button>
<button type="button" data-archive-action="next">Следующий →</button></div>
<details class="archive-picker"><summary>Архив</summary><div class="archive-month-controls">
<button type="button" data-archive-month="previous" aria-label="Предыдущий месяц">←</button>
<span class="archive-month-label" aria-live="polite"></span>
<button type="button" data-archive-month="next" aria-label="Следующий месяц">→</button></div>
<p class="archive-status" role="status" aria-live="polite"></p><div class="archive-panel"></div></details>
</div></nav>
<p class="timezone-note">{html.escape(timezone_note(report))}</p>
<div class="debug-only"><p><strong>ID:</strong> <code>{html.escape(report.id)}</code></p>
<p><strong>Период:</strong> {period}</p>
<p><strong>AI-интерпретация:</strong> {"да" if report.ai_used else "нет"};
{html.escape(report.algorithm_version)}</p></div>
</header><main id="report" class="report-layout">
<div class="overview">{ui.hero(report)}{ui.kpis(report)}{ui.gas_period_card(report)}
{render_charts(report, chart_data, panel_ids=("climate",))}</div>
<aside class="actions"><span class="eyebrow">СЛЕДУЮЩИЙ ШАГ</span><h2>Что делать</h2>
{recommendations or '<p>Рекомендаций за этот период нет.</p>'}</aside>

<section class="thermal-system full-width"><h2>Тепловая система</h2>
<p class="boiler-context">Один котёл · отопление и горячая вода</p>
<div class="thermal-columns"><section><h3>Отопление</h3>{mode_context}{summer_context}</section>
<section>{dhw_context or '<h3>ГВС</h3><p>Недостаточно данных о работе ГВС за период.</p>'}</section></div>
{thermal_interaction}
<div class="engineering-chart">{render_charts(report, chart_data, panel_ids=("thermal",))}</div>
</section>
<div class="lower-grid full-width">{ui.timeline(report)}{ui.quality(report)}</div>
{more_actions}
<section class="details-area full-width"><h2>Почему сделаны эти выводы</h2>
{question_html}{reasoning or '<p>Дополнительные объяснения за период не сформированы.</p>'}
</section>
<div class="details-area full-width">{ui.metric_groups(report, mttr_reason)}{ui.sensors(report)}{sensor_context}
<div class="debug-only">{temporal_evidence}{historical_evidence}</div>
<details class="debug-only"><summary>Канонический JSON</summary><pre>{canonical}</pre></details></div>
{ui.gas_savings_section(report)}
{period_context}
{regeneration}
<section class="full-width owner-settings" aria-label="Профиль и показания">{owner_forms}</section>
</main><footer>Период: {period} · ZontAnalyzer</footer>
<script>{SCRIPT}</script>
<script>{_ARCHIVE_NAVIGATION_SCRIPT}</script>
<script>
(() => {{
  const configuredApiBase = document.body.dataset.feedbackApiBase || "/api";
  const archiveRoot = window.ZontArchive?.root || "/";
  const apiBase = configuredApiBase === "/api" || configuredApiBase === "/za/api"
    ? `${{archiveRoot}}api`.replace(/\\/{{2,}}/g, "/")
    : configuredApiBase;
  const labels = {{applied: "Выполнено", rejected: "Отклонено", ignored: "Без реакции", new: "Новая"}};

  function localExperimentTime(value, timezone) {{
    const parts = new Intl.DateTimeFormat("sv-SE", {{
      timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    }}).formatToParts(new Date(value));
    const at = (kind) => parts.find((part) => part.type === kind).value;
    return `${{at("year")}}-${{at("month")}}-${{at("day")}}T${{at("hour")}}:${{at("minute")}}:${{at("second")}}`;
  }}

  function experimentDisplay(value) {{
    return value == null ? "" : typeof value === "object" ? JSON.stringify(value) : String(value);
  }}

  function applyState(card, payload) {{
    const status = payload.status || "new";
    const note = payload.owner_note || "";
    const statusNode = card.querySelector(".feedback-status");
    statusNode.textContent = labels[status] || status;
    statusNode.dataset.status = status;
    statusNode.className = `feedback-status status-${{status}}`;
    card.querySelector(".feedback-note").value = note;
    card.experimentState = payload.experiment || {{}};
    card.querySelectorAll("[data-experiment-field]").forEach((input) => {{
      const value = payload.experiment?.[input.dataset.experimentField];
      input.value = input.dataset.experimentField === "performed_at" && value
        ? localExperimentTime(value, card.querySelector(".feedback-experiment").dataset.timezone)
        : experimentDisplay(value);
      input.dataset.savedValue = input.value;
    }});
    card.querySelector(".saved-note span").textContent = note || "нет";
    card.querySelector("[data-feedback-save-note]").hidden = !["applied", "rejected"].includes(status);
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

  if (window.location.protocol === "file:") return;
  document.querySelectorAll(".recommendation[data-recommendation-id]").forEach((card) => {{
    const id = card.dataset.recommendationId;
    if (!id) return;
    const message = card.querySelector(".feedback-message");
    card.querySelectorAll("button[data-feedback-status], button[data-feedback-save-note]").forEach((button) => {{
      button.addEventListener("click", async () => {{
        const buttons = card.querySelectorAll("button[data-feedback-status], button[data-feedback-save-note]");
        buttons.forEach((item) => item.disabled = true);
        message.className = "feedback-message";
        message.textContent = "Сохраняю…";
        try {{
          const status = button.dataset.feedbackStatus || card.querySelector(".feedback-status").dataset.status;
          const experiment = {{}};
          let experimentChanged = false;
          card.querySelectorAll("[data-experiment-field]").forEach((input) => {{
            const field = input.dataset.experimentField;
            const value = input.value.trim();
            const changed = input.value !== (input.dataset.savedValue || "");
            experimentChanged ||= changed;
            if (value) experiment[field] = !changed && card.experimentState?.[field] != null
              ? card.experimentState[field] : value;
          }});
          await request(card, {{
            method: "PUT",
            headers: {{"Content-Type": "application/json"}},
            body: JSON.stringify({{
              status,
              ...(status === "applied" && experimentChanged ? {{experiment}} : {{}}),
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
      message.textContent = "Не удалось обновить статус. Показаны сохранённые данные отчёта.";
    }});
  }});
}})();
</script></body></html>"""
