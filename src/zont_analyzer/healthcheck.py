"""Worker heartbeat probe using only the Python standard library."""

from __future__ import annotations

import argparse
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status-file", type=Path, default=Path("/data/worker-status.json"))
    parser.add_argument("--max-age-seconds", type=int, default=900)
    args = parser.parse_args()
    if args.max_age_seconds < 1:
        parser.error("--max-age-seconds must be positive")
    result = worker_health(args.status_file, max_age_seconds=args.max_age_seconds)
    print(json.dumps(result))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
