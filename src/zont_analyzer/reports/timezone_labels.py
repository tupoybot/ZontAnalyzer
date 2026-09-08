"""Human-readable labels for timezones displayed in reports.

The timezone value itself remains an IANA identifier (including the fixed
``Etc/GMT`` identifiers produced from ZONT offsets).  This module only turns
the fixed ZONT offsets into the names used by the ZONT application.
"""
from __future__ import annotations

import re

_ETC_GMT = re.compile(r"^Etc/GMT([+-])(\d+)$")

_ZONT_LABELS: dict[int, str] = {
    0: "Западноевропейское время",
    1: "Центральноевропейское время",
    2: "Калининградское время",
    3: "Московское время",
    4: "Самара, Удмуртия",
    5: "Екатеринбург, Пермь, Курган, Тюмень",
    6: "Омск",
    7: "Новосибирск, Томск, Красноярск, Кемерово",
    8: "Иркутск, Бурятия",
    9: "Якутия, Забайкальский край, Амурская область",
    10: "Владивосток, Хабаровск",
    11: "Магадан, Южно-Сахалинск",
    12: "Камчатское время, Чукотка",
}


def _zont_offset(zone: str) -> int | None:
    """Return the UTC offset represented by a fixed ZONT zone, if supported."""
    if zone in {"UTC", "GMT", "Etc/UTC", "Etc/GMT"}:
        return 0

    match = _ETC_GMT.fullmatch(zone)
    if match is None:
        return None

    # POSIX uses the opposite sign in Etc/GMT names: Etc/GMT-4 means UTC+4.
    sign, digits = match.groups()
    try:
        magnitude = int(digits)
    except ValueError:
        return None
    offset = magnitude if sign == "-" else -magnitude
    return offset if -12 <= offset <= 14 else None


def timezone_label(zone: str) -> str:
    """Return a report label for a timezone without changing its identifier.

    Fixed whole-hour offsets emitted by ZONT are rendered as ``UTC+N — ...``.
    Other IANA names, unsupported offsets, and malformed or fractional values
    are returned unchanged so presentation never fails because of a label.
    """
    if not isinstance(zone, str):
        return zone

    offset = _zont_offset(zone)
    if offset is None:
        return zone
    label = f"UTC{offset:+d}"
    return f"{label} — {_ZONT_LABELS[offset]}" if offset in _ZONT_LABELS else label
