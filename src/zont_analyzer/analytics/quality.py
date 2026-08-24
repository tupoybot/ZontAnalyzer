from __future__ import annotations

from datetime import datetime
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
) -> QualityResult:
    ordered = _ordered(samples)
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
    if len(positive_gaps) >= 10:
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
    gap_penalty = min(1.0, max_gap / max(accepted_gap * 4, 1))
    jump_penalty = min(1.0, jumps / 5)
    stuck_penalty = max(0.0, (stuck_pct - 50) / 50)
    score = max(0.0, min(1.0, coverage / 100 * (1 - 0.35 * gap_penalty - 0.25 * jump_penalty - 0.2 * stuck_penalty)))
    flags: list[str] = []
    if coverage < 70:
        flags.append("low_coverage")
    if max_gap > accepted_gap * 3:
        flags.append("large_gap")
    if stuck_pct > 80:
        flags.append("possibly_stuck_sensor")
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
