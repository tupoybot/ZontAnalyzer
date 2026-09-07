"""Small crash-safe, cross-process ledger for external AI calls.

The application database deliberately remains the source of persisted call
accounting.  This sidecar only serializes reservations and keeps a successful
structured result available for idempotent retries without another API call.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AILedger:
    def __init__(self, database_path: Path):
        self.path = database_path.with_name(database_path.name + ".ai-ledger.json")
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                state = self._read()
                yield state
                temporary = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
                payload = json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8")
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    os.fchmod(descriptor, 0o600)
                    with os.fdopen(descriptor, "wb") as output:
                        descriptor = -1
                        output.write(payload)
                        output.flush()
                        os.fsync(output.fileno())
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                os.replace(temporary, self.path)
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"entries": {}}
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"AI ledger is corrupt: {self.path}") from exc
        if not isinstance(value, dict) or not isinstance(value.get("entries"), dict):
            raise RuntimeError(f"AI ledger has invalid structure: {self.path}")
        if any(not isinstance(entry, dict) for entry in value["entries"].values()):
            raise RuntimeError(f"AI ledger has invalid entries: {self.path}")
        return value

    def reserve(
        self,
        key: str,
        *,
        budget: int,
        used: int | Callable[[], int],
        estimate: int,
        billing_month: str | None = None,
    ) -> dict[str, Any] | None:
        """Reserve a call, returning a cached entry or ``None`` for a new call."""
        now = time.time()
        billing_month = billing_month or datetime.now(UTC).strftime("%Y-%m")
        with self._locked() as state:
            entries = state["entries"]
            existing = entries.get(key)
            if isinstance(existing, dict) and existing.get("status") in {"success", "failure", "pending"}:
                return existing
            pending = sum(
                int(item.get("reserved_tokens", 0) or 0)
                for item in entries.values()
                if item.get("status") == "pending" and item.get("billing_month") == billing_month
            )
            charged = sum(
                int(item.get("charged_tokens", 0) or 0)
                for item in entries.values()
                if item.get("billing_month") == billing_month
            )
            used_tokens = used() if callable(used) else used
            if used_tokens + pending + charged + estimate > budget:
                raise RuntimeError("Monthly OpenAI token budget is exhausted (including reservations)")
            entries[key] = {
                "status": "pending",
                "created_at": now,
                "reserved_tokens": estimate,
                "billing_month": billing_month,
            }
        return None

    def finish(
        self,
        key: str,
        *,
        status: str,
        input_tokens: int,
        output_tokens: int,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        charge_reserved: bool = False,
    ) -> None:
        with self._locked() as state:
            previous = state["entries"].get(key, {})
            state["entries"][key] = {
                "status": status,
                "created_at": previous.get("created_at", time.time()),
                "input_tokens": max(0, int(input_tokens)),
                "output_tokens": max(0, int(output_tokens)),
                "billing_month": previous.get("billing_month", datetime.now(UTC).strftime("%Y-%m")),
                **({"charged_tokens": int(previous.get("reserved_tokens", 0) or 0)} if charge_reserved else {}),
                **({"result": result} if result is not None else {}),
                **({"error": error} if error else {}),
            }
