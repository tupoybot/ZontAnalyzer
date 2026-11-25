"""Calendar gas totals from saved, completed daily reports."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from math import fsum, isfinite
from typing import Any
from zoneinfo import ZoneInfo

from zont_analyzer.analytics.burner_usage import burner_usage
from zont_analyzer.application.gas_cost import calculate_gas_cost, scale_cost, sum_daily_gas_costs
from zont_analyzer.domain import Report
from zont_analyzer.domain.periods import Period, midnight

CALENDAR_GAS_KINDS = frozenset({"weekly", "monthly", "seasonal"})
VERSION = "gas-daily-aggregation-v1"
MAX_DAYS = 400


def expected_days(period: Period) -> list[tuple[date, datetime, datetime]]:
    """Use whole local days within the observed interval, including 23/25-hour days."""
    day = period.start.astimezone(ZoneInfo(period.timezone)).date()
    result = []
    while True:
        start = midnight(day, period.timezone)
        end = midnight(day + timedelta(days=1), period.timezone)
        if start >= period.start and end <= period.observed_end:
            result.append((day, start, end))
            if len(result) > MAX_DAYS:
                raise ValueError(f"Daily report aggregation is limited to {MAX_DAYS} complete local days")
        if end >= period.observed_end:
            return result
        day += timedelta(days=1)


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value >= 0:
        return float(value)
    return None


def _sum_known(rows: Iterable[dict[str, Any]], field: str, *, require_all: bool = False) -> float | None:
    values = [_number(row.get(field)) for row in rows]
    known = [value for value in values if value is not None]
    if not known or (require_all and len(known) != len(values)):
        return None
    return fsum(known)


def _observed_hours(day: dict[str, Any], duration_hours: float) -> float:
    observed_hours = _number(day.get("observed_hours"))
    if observed_hours is None:
        observed_hours = (_number(day.get("coverage_pct")) or 0.0) / 100 * duration_hours
    return min(duration_hours, observed_hours)


class DailyGasAggregation:
    def __init__(self, period: Period):
        self.period = period
        self._expected = {start: (day, end) for day, start, end in expected_days(period)}
        self._rows: dict[datetime, dict[str, Any]] = {}
        self._savings: tuple[datetime, str, dict[str, Any]] | None = None

    def include(self, report: Report) -> None:
        expected = self._expected.get(report.period_start)
        if (
            report.kind != "daily" or expected is None or report.period_start in self._rows
            or report.period_end != expected[1] or report.generated_at < report.period_end
        ):
            return
        raw = report.context.get("gas")
        gas = raw if isinstance(raw, dict) and raw.get("complete", True) else {}
        status = gas.get("status")
        volume = None if status == "unknown" else _number(gas.get("volume_m3"))
        self._rows[report.period_start] = {
            "report_id": report.id,
            "volume_m3": volume,
            "status": (status or "estimated") if volume is not None else "unknown",
            "scope": gas.get("scope") or "unknown",
            "model_version": gas.get("model_version"),
            "algorithm_version": gas.get("algorithm_version"),
            "coverage_pct": _number(gas.get("coverage_pct")),
            "lower_m3": _number(gas.get("lower_m3")),
            "upper_m3": _number(gas.get("upper_m3")),
            "cost": deepcopy(gas.get("cost")),
            "purpose_split": deepcopy(gas.get("purpose_split")),
            **{
                name: _number(gas.get(name))
                for name in ("observed_hours", "flame_hours", "heating_flame_hours", "dhw_flame_hours",
                             "purpose_unknown_flame_hours")
            },
        }
        savings = report.context.get("gas_savings")
        if isinstance(savings, dict) and (self._savings is None or report.period_end > self._savings[0]):
            self._savings = (report.period_end, report.id, savings)

    def contexts(self) -> tuple[dict[str, Any], dict[str, Any]]:
        period_hours = (
            self.period.observed_end.astimezone(UTC) - self.period.start.astimezone(UTC)
        ).total_seconds() / 3600
        daily = []
        for start, (day, end) in self._expected.items():
            saved = self._rows.get(start, {})
            daily.append({
                "day": day.isoformat(), "start": start.isoformat(), "end": end.isoformat(),
                "complete": True, "report_id": None, "status": "unknown", "volume_m3": None,
                "coverage_pct": None,
                **saved,
            })
        known = [row for row in daily if row["volume_m3"] is not None]
        versions = Counter(row.get("model_version") for row in known)
        algorithm_versions = Counter(row.get("algorithm_version") for row in known)
        groups = _scope_groups(known)
        missing = [row["day"] for row in daily if row["volume_m3"] is None]
        included_hours = fsum(
            (end - start).total_seconds() / 3600 for start, (_day, end) in self._expected.items()
        )
        complete_volume = (
            abs(included_hours - period_hours) < 1e-8 and not missing
            and all(row["status"] != "partial" for row in known)
        )
        scope = next(iter(groups)) if len(groups) == 1 else "mixed" if groups else "unknown"
        volume = _sum_known(known, "volume_m3") if len(groups) == 1 else None
        if not known:
            status = "unknown"
        elif volume is None or not complete_volume:
            status = "partial"
        elif all(row["status"] == "measured" for row in known):
            status = "measured"
        elif any(row["status"] == "extrapolated" for row in known):
            status = "extrapolated"
        else:
            status = "estimated"
        day_versions = [row for row in daily if row["report_id"]]
        fingerprint = sha256(json.dumps(
            [(row["report_id"], row["volume_m3"], row["status"], row.get("model_version")) for row in day_versions],
            ensure_ascii=False, separators=(",", ":"),
        ).encode()).hexdigest()[:24]
        observed_hours = fsum(
            _observed_hours(row, (end - start).total_seconds() / 3600)
            for (start, (_day, end)), row in zip(self._expected.items(), daily, strict=True)
        )
        result: dict[str, Any] = {
            "id": f"gas:{int(self.period.start.timestamp())}:{int(self.period.observed_end.timestamp())}:{fingerprint}",
            "epistemic_level": "derived",
            "algorithm_version": VERSION,
            "model_version": next(iter(versions)) if len(versions) == 1 else None,
            "scope": scope,
            "status": status,
            "volume_m3": volume,
            "lower_m3": _sum_known(known, "lower_m3", require_all=True) if volume is not None else None,
            "upper_m3": _sum_known(known, "upper_m3", require_all=True) if volume is not None else None,
            "coverage_pct": min(100.0, observed_hours / period_hours * 100),
            "observed_days": len(daily),
            "observed_hours": observed_hours,
            "average_daily_m3": volume / len(daily) if complete_volume and volume is not None else None,
            "average_weekly_m3": volume / len(daily) * 7 if complete_volume and volume is not None else None,
            "complete": self.period.complete,
            "source": "Сохранённые оценки и измерения завершённых дневных отчётов.",
            "uncertainty_method": "Сумма границ сохранённых оценок за включённые дни.",
            "reasons": [],
            "daily": daily,
            "ai_stale": False,
            **{
                name: _sum_known(daily, name)
                for name in ("flame_hours", "heating_flame_hours", "dhw_flame_hours", "purpose_unknown_flame_hours")
            },
            "aggregation": {
                "expected_days": len(daily),
                "available_days": len(known),
                "missing_days": missing,
                "missing_report_days": [row["day"] for row in daily if row["report_id"] is None],
                "unavailable_days": [row["day"] for row in daily if row["report_id"] and row["volume_m3"] is None],
                "complete_volume": complete_volume and len(groups) == 1,
                "model_versions": _versions(versions),
                "algorithm_versions": _versions(algorithm_versions),
            },
        }
        purpose = _purpose_split(daily, period_hours=period_hours, timezone=self.period.timezone)
        if purpose is not None:
            result["purpose_split"] = purpose
        result.update(burner_usage(result, period_hours))
        _set_costs(result, self.period.timezone)
        saved_savings: dict[str, Any] = {
            "status": "unknown", "comparisons": [],
            "reason": "В сохранённых дневных отчётах за период нет оценки экономии.",
        }
        if self._savings:
            saved_savings = deepcopy(self._savings[2])
            saved_savings["source_report_id"] = self._savings[1]
        return result, saved_savings


def _scope_groups(rows: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups = defaultdict(list)
    for row in rows:
        if row.get("volume_m3") is not None:
            groups[str(row.get("scope") or "unknown")].append(row)
    return dict(groups)


def _versions(versions: Counter[str | None]) -> list[dict[str, Any]]:
    return [{"version": version, "days": versions[version]} for version in sorted(versions, key=str)]


def _purpose_parts(day: dict[str, Any]) -> dict[str, Any]:
    value = day.get("purpose_split")
    return value if isinstance(value, dict) else {}


def _purpose_component(split: dict[str, Any], name: str) -> dict[str, Any]:
    components = split.get("components")
    value = components.get(name) if isinstance(components, dict) else None
    return value if isinstance(value, dict) else {}


def _purpose_split(daily: list[dict[str, Any]], *, period_hours: float, timezone: str) -> dict[str, Any] | None:
    source = [(_purpose_parts(day), day) for day in daily]
    source = [(split, day) for split, day in source if split.get("status") in {"estimated", "partial"}]
    scopes = {split.get("scope") for split, _day in source}
    if len(scopes) != 1:
        return None
    count = len(source)
    complete = (
        count == len(daily) and all(split["status"] == "estimated" for split, _day in source)
        and abs(fsum(
            (datetime.fromisoformat(day["end"]) - datetime.fromisoformat(day["start"])).total_seconds() / 3600
            for _split, day in source
        ) - period_hours) < 1e-8
    )
    components = {}
    for name in ("heating", "dhw", "purpose_unknown"):
        rows = [
            {"start": day["start"], "end": day["end"], **_purpose_component(split, name)}
            for split, day in source
        ]
        volume = _sum_known(rows, "volume_m3")
        components[name] = {
            "status": "unknown" if volume is None else "estimated" if complete else "partial",
            "volume_m3": volume,
            "lower_m3": _sum_known(rows, "lower_m3", require_all=True),
            "upper_m3": _sum_known(rows, "upper_m3", require_all=True),
            "flame_minutes": _sum_known(rows, "flame_minutes"),
            "cost": sum_daily_gas_costs(rows, period_hours=period_hours, timezone=timezone),
        }
    result = {
        "scope": next(iter(scopes)),
        "status": "estimated" if complete else "partial",
        "included_days": count,
        "expected_days": len(daily),
        "components": components,
        "total_modelled_m3": _sum_known((split for split, _day in source), "total_modelled_m3"),
        "allocated_observed_m3": _sum_known((split for split, _day in source), "allocated_observed_m3"),
        "unallocated_m3": _sum_known((split for split, _day in source), "unallocated_m3"),
        "reasons": [] if complete else ["incomplete_gas_days"],
    }
    for volume_name, cost_name in (
        ("total_modelled_m3", "cost"), ("unallocated_m3", "unallocated_cost"),
    ):
        cost_rows = [
            {"start": day["start"], "end": day["end"], "volume_m3": split.get(volume_name),
             "cost": split.get(cost_name)}
            for split, day in source
        ]
        result[cost_name] = sum_daily_gas_costs(cost_rows, period_hours=period_hours, timezone=timezone)
    return result


def _set_costs(gas: dict[str, Any], timezone: str) -> None:
    groups = _scope_groups(gas["daily"])
    aggregate = gas["aggregation"]
    aggregate["scope_totals"] = [
        {
            "scope": scope, "days": len(rows), "volume_m3": _sum_known(rows, "volume_m3"),
            "cost": sum_daily_gas_costs(rows, period_hours=gas["period_hours"], timezone=timezone),
        }
        for scope, rows in sorted(groups.items()) if len(groups) > 1
    ]
    gas["cost"] = sum_daily_gas_costs(
        (next(iter(groups.values())) if len(groups) == 1 else []),
        period_hours=gas["period_hours"], timezone=timezone,
        limitation="mixed_gas_scopes" if len(groups) > 1 else "incomplete_gas_days",
    )
    days = aggregate["expected_days"]
    gas["average_daily_cost"] = scale_cost(
        gas["cost"], 1 / days if days else None, basis="average_daily_from_period_cost",
    )
    gas["average_weekly_cost"] = scale_cost(
        gas["cost"], 7 / days if days else None, basis="average_weekly_from_period_cost",
    )


def reprice_aggregate(gas: dict[str, Any], tariffs: list[dict[str, Any]], *, timezone: str) -> None:
    """Explicit tariff changes reprice the same saved local-day volumes."""
    for row in gas["daily"]:
        start, end = datetime.fromisoformat(row["start"]), datetime.fromisoformat(row["end"])
        if row["volume_m3"] is not None:
            row["cost"] = calculate_gas_cost(start, end, row["volume_m3"], tariffs, timezone=timezone)
        split = _purpose_parts(row)
        if split:
            for name in ("heating", "dhw", "purpose_unknown"):
                component = _purpose_component(split, name)
                if component:
                    component["cost"] = calculate_gas_cost(
                        start, end, component.get("volume_m3"), tariffs, timezone=timezone,
                    )
            for volume_name, cost_name in (("total_modelled_m3", "cost"), ("unallocated_m3", "unallocated_cost")):
                split[cost_name] = calculate_gas_cost(start, end, split.get(volume_name), tariffs, timezone=timezone)
    if "purpose_split" in gas:
        gas["purpose_split"] = _purpose_split(gas["daily"], period_hours=gas["period_hours"], timezone=timezone)
    _set_costs(gas, timezone)
