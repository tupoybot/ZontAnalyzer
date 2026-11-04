"""Monotonic queue ages for the local laboratory's acknowledged queue changes.

Fixture publication timestamps belong to a fixed historical wall clock. They
cannot be subtracted from the runner's real wall clock to measure queue waiting.
Only transitions observed after successful repository saves establish age here.
The caller must not call committed() for a failed or rejected save. Identifiers
remain private bookkeeping; the only exported measurement is an age in seconds.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from unittest.mock import patch


class QueueClock:
    """Track dirty cycles from their first acknowledged local monotonic instant."""

    def __init__(self):
        self._pending_since: dict[str, float | None] = {}

    def committed(self, items, previous):
        """Observe successfully saved href-to-item maps, including deletions.

        A preexisting dirty item first observed here has unknown start time, even
        if its stored queued_at timestamp looks usable. Partial maps leave other
        tracked items untouched; age() checks the full pending count separately.
        """
        acknowledged = time.monotonic()
        for href in previous.keys() - items.keys():
            self._pending_since.pop(href, None)
        for href, item in items.items():
            if not item.get("dirty", 0):
                self._pending_since.pop(href, None)
            elif not previous.get(href, {}).get("dirty", 0):
                self._pending_since[href] = acknowledged
            elif href not in self._pending_since:
                self._pending_since[href] = None

    def age(self, pending_count):
        """Return oldest acknowledged dirty age, or None for incomplete coverage."""
        if type(pending_count) is not int or pending_count < 0:
            return None
        if pending_count == 0:
            return 0.0
        if pending_count != len(self._pending_since):
            return None
        starts = list(self._pending_since.values())
        if any(start is None for start in starts):
            return None
        return max(0.0, time.monotonic() - min(starts))


@contextmanager
def observe_publication_queue(clock):
    """Observe existing successful saves without adding database operations."""
    from zont_analyzer.adapters.ydb.publication import PublicationRepository

    original = PublicationRepository.save

    def save(repo, items, previous, *args, **kwargs):
        result = original(repo, items, previous, *args, **kwargs)
        if result is True:
            clock.committed(items, previous)
        return result

    with patch.object(PublicationRepository, "save", save):
        yield
