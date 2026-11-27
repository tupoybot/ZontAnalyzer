"""Precise edges of regulation windows carried in the existing hourly change rows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

LOOKAHEAD_SECONDS = 61
WINDOW_PREFIX = "meta:telemetry-window:"
WINDOW_FORMAT = "%Y-%m-%dT%H:%M:%S"
PARTS_FIELD = "regulation_parts_v1"

# Modern local midnights are on UTC quarter hours. Split each quarter at the
# true regulation lookahead boundary so the next ordinary sample is outside.
PART_EDGES = tuple(edge for quarter in range(0, 3600, 900) for edge in (quarter, quarter + LOOKAHEAD_SECONDS)) + (3600,)


def hour_bounds(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    first = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    last = end.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    return first, last + timedelta(hours=1) if last < end else last


def supports_precision(value: datetime) -> bool:
    utc = value.astimezone(UTC)
    return not utc.microsecond and utc.minute * 60 + utc.second in PART_EDGES


def window_bounds(key: str) -> tuple[datetime, datetime]:
    left, separator, right = key.removeprefix(WINDOW_PREFIX).partition("/")
    start = datetime.strptime(left, WINDOW_FORMAT).replace(tzinfo=UTC)
    end = datetime.strptime(right, WINDOW_FORMAT).replace(tzinfo=UTC)
    if (not separator or end <= start or left != start.strftime(WINDOW_FORMAT)
            or right != end.strftime(WINDOW_FORMAT) or not all(map(supports_precision, (start, end)))):
        raise ValueError("invalid regulation telemetry window")
    return start, end


def part_revisions(payload: Any, revision: int) -> list[int]:
    parts = payload.get(PARTS_FIELD) if isinstance(payload, dict) else None
    if (isinstance(parts, list) and len(parts) == len(PART_EDGES) - 1
            and all(type(value) is int and 0 <= value <= revision for value in parts) and max(parts) == revision):
        return list(parts)
    # Old hourly rows did not record where within the hour the change occurred.
    return [revision] * (len(PART_EDGES) - 1)
