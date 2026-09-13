"""Worker heartbeat probe using only the Python standard library."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def read_worker_status(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("worker status root must be an object")
    return payload


def worker_health(path: Path, *, max_age_seconds: int, now: datetime | None = None) -> dict[str, Any]:
    checked_at = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        status = read_worker_status(path)
        updated_at = datetime.fromisoformat(str(status["updated_at"]))
        if updated_at.tzinfo is None:
            raise ValueError("updated_at has no timezone")
        age_seconds = max(0.0, (checked_at - updated_at.astimezone(UTC)).total_seconds())
        state = str(status.get("state", "unknown"))
        healthy = state in {"starting", "syncing", "analyzing", "publishing", "ok"} and (
            age_seconds <= max_age_seconds
        )
        return {
            "ok": healthy,
            "state": state,
            "age_seconds": round(age_seconds, 3),
            "max_age_seconds": max_age_seconds,
            "status_file": str(path),
        }
    except (OSError, ValueError, KeyError) as exc:
        return {
            "ok": False,
            "state": "missing_or_invalid",
            "error": f"{type(exc).__name__}: {exc}",
            "max_age_seconds": max_age_seconds,
            "status_file": str(path),
        }
