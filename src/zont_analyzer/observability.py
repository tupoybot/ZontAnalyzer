"""Bounded request-local measurements; never transmit business data or exceptions."""
from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

Measurement = tuple[str, float, dict[str, str]]
Sink = Callable[[Measurement], None]
_sink: ContextVar[Sink | None] = ContextVar("measurement_sink", default=None)
_NAME = re.compile(r"zont_[a-z][a-z0-9_]{0,79}\Z")
_LABEL = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")


def observe(name: str, value: float = 1.0, **labels: str) -> None:
    """Record a fixed-name numeric measurement without affecting application work."""
    sink = _sink.get()
    if sink is None:
        return
    try:
        numeric = float(value)
        if (not _NAME.fullmatch(name) or not math.isfinite(numeric) or numeric < 0
                or len(labels) > 4
                or any(not _LABEL.fullmatch(k) or not _LABEL.fullmatch(v) for k, v in labels.items())):
            return
        sink((name, numeric, labels))
    except Exception:  # noqa: BLE001 - monitoring must not change application outcomes
        return


@contextmanager
def capture(sink: Sink) -> Iterator[None]:
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


@contextmanager
def span(name: str, **labels: str) -> Iterator[None]:
    started = time.monotonic()
    success = False
    try:
        yield
        success = True
    finally:
        observe(name + "_observed_timestamp_seconds", time.time(), **labels)
        observe(name + "_success", float(success), **labels)
        observe(name + "_duration_seconds", time.monotonic() - started, **labels)
