"""Shared YDB lease for long-running cloud work across runtime instances."""

from __future__ import annotations

import math
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from zont_analyzer.adapters.ydb.jobs import JobLease

if TYPE_CHECKING:
    from zont_analyzer.runtime import Runtime


class _Jobs(Protocol):
    def acquire(self, job_key: str, owner: str, lease_seconds: int) -> JobLease | None: ...

    def release(self, job_key: str, owner: str, attempt: int) -> bool: ...


KEY = "cloud-heavy-work:v1"
LEASE_GRACE_SECONDS = 30


@dataclass(frozen=True)
class HeavyWorkLease:
    """A process-independent lease held while a long cloud operation runs."""

    jobs: _Jobs
    lease: JobLease

    @classmethod
    def acquire(
        cls,
        runtime: Runtime,
        *,
        deadline: float,
        monotonic: Callable[[], float] | None = None,
    ) -> HeavyWorkLease | None:
        remaining = deadline - (monotonic or time.monotonic)()
        if remaining <= 0:
            return None
        seconds = math.ceil(remaining) + LEASE_GRACE_SECONDS
        owner = str(uuid.uuid4())
        lease = runtime.db.jobs.acquire(KEY, owner, seconds)
        return cls(runtime.db.jobs, lease) if lease is not None else None

    def release(self) -> None:
        self.jobs.release(KEY, self.lease.owner, self.lease.attempt)
