"""Bounded long-period aggregation from completed canonical daily reports."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from zont_analyzer.adapters.sqlite.database import Database, ReportRow
from zont_analyzer.analytics.evidence import EvidenceMetric
from zont_analyzer.domain import DetectedEvent, MetricValue, QualityResult, Report
from zont_analyzer.domain.periods import Period, midnight

ALGORITHM_VERSION = "long-period-v1"
MAX_DAYS = 400
MAX_WINDOWS = 12
MAX_EVENT_KINDS = 64

_DAILY_STATISTICS = {
    "burner_cycle_median_seconds",
    "burner_cycle_p90_seconds",
    "flame_modulation_mean",
    "flame_modulation_median",
    "flow_vs_cs_typical_c",
    "flow_vs_cs_p90_c",
    "delta_t_c",
}
_LATEST_CONTEXT = ("sensors", "dhw_interaction", "reliability")


def _expected_days(period: Period) -> list[tuple[date, datetime, datetime]]:
    timezone = ZoneInfo(period.timezone)
    day = period.start.astimezone(timezone).date()
    result: list[tuple[date, datetime, datetime]] = []
    while True:
        start = midnight(day, period.timezone)
        end = midnight(day + timedelta(days=1), period.timezone)
        if start >= period.start and end <= period.observed_end:
            result.append((day, start, end))
        if end >= period.observed_end:
            break
        day += timedelta(days=1)
        if len(result) > MAX_DAYS:
            raise ValueError(f"Long-period aggregation is limited to {MAX_DAYS} complete local days")
    return result


def _daily_evidence(report: Report) -> list[EvidenceMetric]:
    raw = report.context.get("temporal_evidence", {}).get("metrics", [])
    result: list[EvidenceMetric] = []
    for item in raw:
        try:
            result.append(EvidenceMetric.model_validate(item))
        except (TypeError, ValueError):
            continue
    return result


def _role_coverage(report: Report, role: str) -> float | None:
    temporal = report.context.get("temporal_evidence", {})
    signals = temporal.get("signals", {})
    quality = temporal.get("quality", {})
    values = [
        quality.get(key, {}).get("coverage_pct")
        for key, metadata in signals.items()
        if isinstance(metadata, dict) and metadata.get("role") == role
    ]
    numeric = [float(value) for value in values if isinstance(value, (int, float))]
    return max(numeric) if numeric else None


def _longest_missing_seconds(
    expected: list[tuple[date, datetime, datetime]], included: set[str]
) -> float:
    longest = current = 0.0
    for day, start, end in expected:
        if day.isoformat() in included:
            current = 0.0
        else:
            current += (end - start).total_seconds()
            longest = max(longest, current)
    return longest


def _evenly_selected(values: list[dict[str, Any]], maximum: int = MAX_WINDOWS) -> list[dict[str, Any]]:
    if len(values) <= maximum:
        return values
    indexes = sorted({round(index * (len(values) - 1) / (maximum - 1)) for index in range(maximum)})
    return [values[index] for index in indexes]


def aggregate_long_period(db: Database, period: Period) -> Report:
    """Aggregate a long period without loading raw telemetry or daily AI prose.

    Only canonical daily reports whose exact local-day boundaries are fully
    contained in ``period`` are eligible.  The query is streamed and the
    retained numeric collections are bounded by the calendar contract.
    """
    if period.kind not in {"monthly", "seasonal"}:
        raise ValueError("Long-period aggregation supports monthly and seasonal periods")
    expected = _expected_days(period)
    expected_by_start = {int(start.timestamp()): (day, end) for day, start, end in expected}
    expected_seconds = sum((end - start).total_seconds() for _day, start, end in expected)

    additive: dict[tuple[str, str], float] = defaultdict(float)
    additive_days: dict[tuple[str, str], int] = defaultdict(int)
    means: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    rates: dict[tuple[str, str, str], list[tuple[float, float]]] = defaultdict(list)
    daily_statistics: dict[str, list[float]] = defaultdict(list)
    event_counts: dict[str, dict[str, Any]] = {}
    day_windows: list[dict[str, Any]] = []
    included_dates: list[str] = []
    seen_starts: set[int] = set()
    weighted_quality = weighted_coverage = weighted_stuck = included_seconds = 0.0
    max_gap = 0.0
    jumps = sample_count = 0
    latest_context: dict[str, Any] = {}
    latest_report_id: str | None = None
    latest_end: datetime | None = None

    statement = (
        select(ReportRow.canonical_json)
        .where(
            ReportRow.kind == "daily",
            ReportRow.period_start >= int(period.start.timestamp()),
            ReportRow.period_end <= int(period.observed_end.timestamp()),
        )
        .order_by(ReportRow.period_start, ReportRow.generated_at.desc())
        .execution_options(yield_per=1)
    )
    with db.session() as session:
        for encoded in session.scalars(statement):
            report = Report.model_validate_json(encoded)
            start_key = int(report.period_start.timestamp())
            expected_day = expected_by_start.get(start_key)
            if expected_day is None or start_key in seen_starts:
                continue
            day, expected_end = expected_day
            if report.period_end != expected_end or report.generated_at < report.period_end:
                continue
            seen_starts.add(start_key)
            duration = (report.period_end - report.period_start).total_seconds()
            included_seconds += duration
            included_dates.append(day.isoformat())
            weighted_quality += report.quality.score * duration
            weighted_coverage += report.quality.coverage_pct * duration
            weighted_stuck += report.quality.stuck_pct * duration
            max_gap = max(max_gap, report.quality.max_gap_seconds)
            jumps += report.quality.implausible_jumps
            sample_count += report.quality.sample_count

            metric_values = {item.name: item.value for item in report.metrics}
            for report_metric in report.metrics:
                key = (report_metric.name, report_metric.unit)
                if report_metric.unit == "count" or report_metric.unit == "°C·h":
                    additive[key] += report_metric.value
                    additive_days[key] += 1
                elif report_metric.name.endswith("mean_temperature_c"):
                    role = (
                        "outdoor_temperature"
                        if report_metric.name.startswith("outdoor_")
                        else "control_temperature"
                    )
                    coverage = _role_coverage(report, role)
                    if coverage is None and role == "control_temperature":
                        coverage = report.quality.coverage_pct
                    if coverage is not None and coverage > 0:
                        means[key].append((report_metric.value, duration * coverage / 100))

            evidence = _daily_evidence(report)
            evidence_by_name = {item.name: item for item in evidence}
            for evidence_metric in evidence:
                if evidence_metric.value is None:
                    continue
                if evidence_metric.name in _DAILY_STATISTICS:
                    daily_statistics[evidence_metric.name].append(evidence_metric.value)
                if (
                    evidence_metric.denominator is not None
                    and evidence_metric.denominator > 0
                    and evidence_metric.denominator_unit
                    and evidence_metric.unit in {"ratio", "count/hour"}
                ):
                    rates[(evidence_metric.name, evidence_metric.unit, evidence_metric.denominator_unit)].append(
                        (evidence_metric.value, evidence_metric.denominator)
                    )
            runtime_metric = evidence_by_name.get("burner_runtime_request_ratio")
            temporal_windows = report.context.get("temporal_evidence", {}).get("windows", [])
            delta_values = [
                value
                for item in temporal_windows
                if not item.get("excluded_reasons")
                for value in [item.get("facts", {}).get("delta_t_c", {}).get("mean")]
                if isinstance(value, (int, float))
            ]
            if delta_values:
                daily_statistics["delta_t_c"].append(median(delta_values))

            for event in report.events:
                entry = event_counts.setdefault(
                    event.kind,
                    {"count": 0, "severity": "info", "first": event.started_at, "last": event.started_at},
                )
                entry["count"] += 1
                entry["first"] = min(entry["first"], event.started_at)
                entry["last"] = max(entry["last"], event.ended_at or event.started_at)
                if {"info": 0, "warning": 1, "critical": 2}[event.severity] > {
                    "info": 0, "warning": 1, "critical": 2
                }[entry["severity"]]:
                    entry["severity"] = event.severity

            day_windows.append(
                {
                    "id": f"evidence:long:{period.kind}:{start_key}:{ALGORITHM_VERSION}",
                    "started_at": report.period_start.isoformat(),
                    "ended_at": report.period_end.isoformat(),
                    "timezone": period.timezone,
                    "kind": "representative",
                    "tags": ["daily_aggregate"],
                    "excluded_reasons": [],
                    "signals": {},
                    "facts": {
                        name: {"mean": value, "source": "derived"}
                        for name, value in (
                            ("outdoor_mean_temperature_c", metric_values.get("outdoor_mean_temperature_c")),
                            ("mean_temperature_c", metric_values.get("mean_temperature_c")),
                            (
                                "burner_runtime_request_ratio",
                                runtime_metric.value if runtime_metric is not None else None,
                            ),
                        )
                        if isinstance(value, (int, float))
                    },
                    "source_report_id": report.id,
                    "epistemic_level": "derived_from_daily_report",
                }
            )
            if latest_end is None or report.period_end > latest_end:
                latest_end = report.period_end
                latest_report_id = report.id
                latest_context = {
                    key: deepcopy(report.context[key]) for key in _LATEST_CONTEXT if key in report.context
                }

    expected_dates = [day.isoformat() for day, _start, _end in expected]
    included_set = set(included_dates)
    missing_dates = [day for day in expected_dates if day not in included_set]
    if missing_dates:
        max_gap = max(max_gap, _longest_missing_seconds(expected, included_set))
    calendar_coverage = included_seconds / expected_seconds if expected_seconds else 0.0
    score = min(calendar_coverage, weighted_quality / included_seconds if included_seconds else 0.0)
    coverage_pct = weighted_coverage / expected_seconds if expected_seconds else 0.0
    flags = []
    if missing_dates:
        flags.append("missing_daily_reports")
    if not included_dates:
        flags.append("no_completed_daily_reports")

    report_id = f"report:{period.kind}:{int(period.start.timestamp())}:report-v2"
    metrics: list[MetricValue] = []
    for (name, unit), value in sorted(additive.items()):
        metrics.append(
            MetricValue(
                id=f"metric:{report_id}:{name}:{ALGORITHM_VERSION}",
                name=name,
                value=round(value, 4),
                unit=unit,
                algorithm_version=ALGORITHM_VERSION,
                context={"aggregation": "sum_of_known_daily_values", "days": additive_days[(name, unit)]},
            )
        )
    for (name, unit), values in sorted(means.items()):
        denominator = sum(weight for _value, weight in values)
        metrics.append(
            MetricValue(
                id=f"metric:{report_id}:{name}:{ALGORITHM_VERSION}",
                name=name,
                value=round(sum(value * weight for value, weight in values) / denominator, 4),
                unit=unit,
                algorithm_version=ALGORITHM_VERSION,
                context={"aggregation": "signal_coverage_weighted_daily_mean", "days": len(values)},
            )
        )

    evidence_metrics = [
        EvidenceMetric(
            id=f"metric:{report_id}:{name}:{ALGORITHM_VERSION}",
            name=name,
            value=round(sum(value * weight for value, weight in values) / sum(weight for _value, weight in values), 4),
            unit=unit,
            source="derived",
            denominator=round(sum(weight for _value, weight in values), 4),
            denominator_unit=denominator_unit,
            coverage_pct=round(calendar_coverage * 100, 2),
        ).model_dump(mode="json", exclude_none=True)
        for (name, unit, denominator_unit), values in sorted(rates.items())
    ]
    events = [
        DetectedEvent(
            id=f"event:{report_id}:{kind}:{ALGORITHM_VERSION}",
            kind="period_event_summary",
            started_at=value["first"],
            ended_at=value["last"],
            severity=value["severity"],
            details={
                "count": value["count"],
                "original_kind": kind,
                "aggregation": "count_of_events_in_completed_daily_reports",
                "epistemic_level": "derived",
            },
            algorithm_version=ALGORITHM_VERSION,
        )
        for kind, value in sorted(event_counts.items())[:MAX_EVENT_KINDS]
    ]
    daily_summary = {
        name: {
            "median_of_daily_values": round(median(values), 4),
            "minimum_daily_value": round(min(values), 4),
            "maximum_daily_value": round(max(values), 4),
            "days": len(values),
            "scope": "daily_statistic; not a seasonal sample quantile",
        }
        for name, values in sorted(daily_statistics.items())
    }
    context: dict[str, Any] = {
        "period": period.model_dump(mode="json"),
        "long_period_aggregation": {
            "algorithm_version": ALGORITHM_VERSION,
            "source": "completed canonical daily reports; no raw telemetry or daily AI text",
            "expected_days": len(expected_dates),
            "included_days": len(included_dates),
            "expected_duration_seconds": expected_seconds,
            "included_duration_seconds": included_seconds,
            "included_dates": included_dates,
            "missing_dates": missing_dates,
            "daily_statistics": daily_summary,
            "limitations": [
                "Missing daily reports are excluded, never treated as zero.",
                "Daily medians and quantiles are not promoted to period-wide sample quantiles.",
            ],
        },
        "temporal_evidence": {
            "algorithm_version": ALGORITHM_VERSION,
            "period_start": period.start.isoformat(),
            "period_end": period.observed_end.isoformat(),
            "timezone": period.timezone,
            "windows": _evenly_selected(day_windows),
            "metrics": evidence_metrics,
            "quality": {},
            "signals": {},
            "exclusions": {},
            "unknowns": (["missing:daily_reports"] if missing_dates else []),
        },
        "latest_daily_context": {
            "source_report_id": latest_report_id,
            "observed_end": latest_end.isoformat() if latest_end else None,
            "scope": "latest completed daily context; not a period-wide historical fact",
            "fields": sorted(latest_context),
            "values": {key: value for key, value in latest_context.items() if key != "sensors"},
        },
        **({"sensors": latest_context["sensors"]} if "sensors" in latest_context else {}),
    }
    summary = (
        f"Длинный период собран из {len(included_dates)} из {len(expected_dates)} завершённых "
        f"суточных отчётов; отсутствует {len(missing_dates)}. Показаны наблюдаемые факты; "
        "отсутствующие дни не считаются нулевыми."
    )
    return Report(
        id=report_id,
        kind=period.kind,
        period_start=period.start,
        period_end=period.observed_end,
        generated_at=datetime.now(UTC),
        timezone=period.timezone,
        context=context,
        quality=QualityResult(
            score=round(score, 4),
            coverage_pct=round(min(100.0, coverage_pct), 2),
            max_gap_seconds=max_gap,
            stuck_pct=round(weighted_stuck / included_seconds, 2) if included_seconds else 0.0,
            implausible_jumps=jumps,
            sample_count=sample_count,
            flags=flags,
        ),
        metrics=metrics,
        events=events,
        recommendations=[],
        summary=summary,
        ai_used=False,
    )


__all__ = ["aggregate_long_period"]
