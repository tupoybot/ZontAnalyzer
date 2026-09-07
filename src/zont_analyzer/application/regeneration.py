"""Background, durable report regeneration jobs.

The job state is kept in the existing SQLite application metadata and the
per-report lock is an OS file lock.  This makes the HTTP endpoint safe when
the application is served by more than one process without adding another
database schema migration.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zont_analyzer.application.pilot import atomic_write_text, reports_directory
from zont_analyzer.application.publication import _publish_locked
from zont_analyzer.reports import render_text
from zont_analyzer.runtime import Runtime

logger = logging.getLogger(__name__)
_PREFIX = "report-regeneration:"
_thread_lock = threading.Lock()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _key(report_id: str) -> str:
    return _PREFIX + report_id


def _read(runtime: Runtime, report_id: str) -> dict[str, Any] | None:
    raw = runtime.db.get_app_meta(_key(report_id))
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write(runtime: Runtime, report_id: str, value: dict[str, Any]) -> None:
    runtime.db.set_app_meta(_key(report_id), json.dumps(value, ensure_ascii=False, sort_keys=True))


def _publication_snapshot(runtime: Runtime) -> dict[Path, tuple[bytes, int]]:
    root = reports_directory(runtime)
    if not root.exists():
        return {}
    snapshot: dict[Path, tuple[bytes, int]] = {}
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in {".html", ".json"}:
            snapshot[path.relative_to(root)] = (path.read_bytes(), os.stat(path).st_mode & 0o777)
    return snapshot


def _restore_publication(runtime: Runtime, snapshot: dict[Path, tuple[bytes, int]]) -> None:
    root = reports_directory(runtime)
    current = {
        path.relative_to(root)
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".html", ".json"}
    } if root.exists() else set()
    for relative in current - snapshot.keys():
        (root / relative).unlink(missing_ok=True)
    for relative, (content, mode) in snapshot.items():
        target = root / relative
        atomic_write_text(target, content.decode("utf-8"), mode=mode)


def status(runtime: Runtime, report_id: str) -> dict[str, Any]:
    value = _read(runtime, report_id)
    value = value or {"report_id": report_id, "status": "idle"}
    if value.get("status") in {"queued", "running"}:
        path = _lock_path(runtime, report_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return value
            # The job may have finished between the first DB read and this lock.
            completed = _read(runtime, report_id)
            if completed and completed.get("status") not in {"queued", "running"}:
                return completed
            value = {
                "report_id": report_id, "status": "error", "updated_at": _now(),
                "error": "Задание прервано после перезапуска приложения.",
            }
            _write(runtime, report_id, value)
    return value


def _lock_path(runtime: Runtime, report_id: str) -> Path:
    # The report id is generated internally, but keep the path safe for old
    # databases containing a hand-created report id.
    safe = "".join(ch if ch.isalnum() or ch in ".-_" else "_" for ch in report_id)
    return reports_directory(runtime) / (".regenerate-" + safe + ".lock")


def _run(runtime: Runtime, report_id: str, lock: Any) -> None:
    old = runtime.db.report(report_id)
    if old is None:
        _write(runtime, report_id, {
            "report_id": report_id, "status": "error", "error": "Отчёт не найден.", "updated_at": _now(),
        })
        return
    _write(runtime, report_id, {"report_id": report_id, "status": "running", "updated_at": _now()})
    try:
        # AnalysisService.regenerate is deliberately the single integration
        # point: it computes fresh evidence/AI and does not persist the
        # candidate until this job has accepted it.
        candidate = runtime.analysis().regenerate(old, request_nonce=_now())
        # Serialize the complete commit with worker and feedback publication.
        output_dir = reports_directory(runtime)
        output_dir.mkdir(parents=True, exist_ok=True)
        with (output_dir / ".publication.lock").open("a") as publication_lock:
            fcntl.flock(publication_lock, fcntl.LOCK_EX)
            snapshot = _publication_snapshot(runtime)
            try:
                _publish_locked(runtime, output_dir, datetime.now(UTC), overrides=[candidate])
                runtime.db.save_report(candidate, render_text(candidate))
            except BaseException:
                _restore_publication(runtime, snapshot)
                raise
            finally:
                fcntl.flock(publication_lock, fcntl.LOCK_UN)
        _write(runtime, report_id, {
            "report_id": report_id, "status": "success", "updated_at": _now(),
            "generated_at": candidate.generated_at.isoformat(),
        })
    except BaseException as exc:  # background failures must become observable state
        logger.exception("Report regeneration failed for %s", report_id)
        _write(runtime, report_id, {
            "report_id": report_id, "status": "error", "updated_at": _now(),
            "error": str(exc)[:500] or type(exc).__name__,
        })


def start(runtime: Runtime, report_id: str) -> dict[str, Any]:
    """Start one job, returning the durable state for duplicate clicks too."""
    report = runtime.db.report(report_id)
    if report is None:
        raise KeyError(report_id)
    if report.kind == "initial":
        raise ValueError("Перегенерация доступна для дневного, недельного, месячного и сезонного отчёта.")
    path = _lock_path(runtime, report_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        current = status(runtime, report_id)
        return current | {"report_id": report_id, "status": current.get("status", "running")}
    current = status(runtime, report_id)
    # A completed result is idempotent until a later explicit click starts a
    # new run; an active run is represented by the held OS lock.
    with _thread_lock:
        _write(runtime, report_id, {"report_id": report_id, "status": "queued", "updated_at": _now()})
        thread = threading.Thread(
            target=_run_and_close, args=(runtime, report_id, lock), daemon=True,
            name=f"regenerate-{report_id}",
        )
        thread.start()
    return status(runtime, report_id)


def _run_and_close(runtime: Runtime, report_id: str, lock: Any) -> None:
    try:
        _run(runtime, report_id, lock)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
