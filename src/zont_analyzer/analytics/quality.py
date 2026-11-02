from __future__ import annotations

from datetime import datetime
from math import isfinite
from statistics import median

from zont_analyzer.domain import QualityResult


def _ordered(samples: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    by_time = {timestamp: value for timestamp, value in samples}
    return sorted(by_time.items())


def assess_quality(
    samples: list[tuple[datetime, float]],
    period_start: datetime,
    period_end: datetime,
    *,
    max_temperature_rate_c_per_hour: float = 10.0,
    expected_interval_seconds: float | None = None,
) -> QualityResult:
    if expected_interval_seconds is not None and (
        not isfinite(expected_interval_seconds) or expected_interval_seconds <= 0
    ):
        raise ValueError("expected_interval_seconds must be finite and positive")
    ordered = _ordered([
        (timestamp, value) for timestamp, value in samples
        if period_start <= timestamp <= period_end
    ])
    period_seconds = max((period_end - period_start).total_seconds(), 1)
    if len(ordered) < 2:
        return QualityResult(
            score=0,
            coverage_pct=0,
            max_gap_seconds=period_seconds,
            stuck_pct=0,
            implausible_jumps=0,
            sample_count=len(ordered),
            flags=["insufficient_samples"],
        )
    gaps = [(ordered[index + 1][0] - ordered[index][0]).total_seconds() for index in range(len(ordered) - 1)]
    positive_gaps = [gap for gap in gaps if gap > 0]
    if expected_interval_seconds is not None:
        # A sensor's known heartbeat remains valid during bursts of changes.
        expected = expected_interval_seconds
    elif len(positive_gaps) >= 10:
        # With enough observations the median is robust to occasional short
        # burst updates. Radio sensors normally report every several minutes,
        # but may emit two close points when the value changes; treating that
        # shortest interval as the cadence creates false coverage gaps.
        expected = median(positive_gaps)
    elif positive_gaps:
        # With only a few points, prevent one large outage from becoming the
        # inferred cadence and making sparse telemetry look complete.
        expected = min(median(positive_gaps), min(positive_gaps) * 3)
    else:
        expected = period_seconds
    if expected_interval_seconds is None:
        # Uniformly sparse observations cannot establish a healthy cadence.
        expected = min(expected, 600)
    accepted_gap = max(expected * 2.5, 60)
    observed_seconds = sum(min(gap, accepted_gap) for gap in positive_gaps)
    coverage = min(100.0, observed_seconds / period_seconds * 100)
    max_gap = max(positive_gaps, default=period_seconds)
    stuck_seconds = 0.0
    jumps = 0
    for index, gap in enumerate(gaps):
        if gap <= 0:
            continue
        before = ordered[index][1]
        after = ordered[index + 1][1]
        if before == after and gap <= accepted_gap:
            stuck_seconds += gap
        rate = abs(after - before) / gap * 3600
        if rate > max_temperature_rate_c_per_hour and abs(after - before) > 1:
            jumps += 1
    stuck_pct = min(100.0, stuck_seconds / max(observed_seconds, 1) * 100)
    jump_penalty = min(1.0, jumps / 5)
    # Missing time already reduces coverage in proportion to the period.
    # Equal consecutive temperatures alone do not establish a faulty sensor.
    score = max(0.0, min(1.0, coverage / 100 * (1 - 0.25 * jump_penalty)))
    flags: list[str] = []
    if coverage < 70:
        flags.append("low_coverage")
    if max_gap > accepted_gap:
        flags.append("large_gap")
    if jumps:
        flags.append("implausible_jumps")
    return QualityResult(
        score=round(score, 4),
        coverage_pct=round(coverage, 2),
        max_gap_seconds=round(max_gap, 1),
        stuck_pct=round(stuck_pct, 2),
        implausible_jumps=jumps,
        sample_count=len(ordered),
        flags=flags,
    )
