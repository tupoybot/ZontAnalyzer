from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from zont_analyzer.application.analysis import CALCULATION_VERSION
from zont_analyzer.application.reasoning_context import reuse_ai_interpretation
from zont_analyzer.domain import Report
from zont_analyzer.reports import render_html, render_text
from zont_analyzer.reports.chart_data import cached_chart_data

if TYPE_CHECKING:
    from zont_analyzer.runtime import Runtime


logger = logging.getLogger(__name__)


class WorkerCycleError(RuntimeError):
    """A worker iteration did not complete and must not be reported as successful."""


def _configured_path(data_dir: Path, configured: str, *, label: str) -> Path:
    if not configured.strip() or "\x00" in configured:
        raise ValueError(f"{label} must be a non-empty filesystem path")
    candidate = Path(configured)
    base = data_dir.resolve()
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (base / candidate).resolve()
        if not resolved.is_relative_to(base):
            raise ValueError(f"Relative {label} must stay inside the data directory")
    if resolved == Path(resolved.anchor):
        raise ValueError(f"{label} must not be the filesystem root")
    return resolved


def reports_directory(runtime: Runtime) -> Path:
    return _configured_path(
        runtime.loaded.data_dir,
        runtime.config.pilot.reports_dir,
        label="pilot.reports_dir",
    )


def worker_status_path(runtime: Runtime) -> Path:
    path = _configured_path(
        runtime.loaded.data_dir,
        runtime.config.pilot.worker_status_file,
        label="pilot.worker_status_file",
    )
    if path.exists() and path.is_dir():
        raise ValueError("pilot.worker_status_file must point to a file")
    return path


def atomic_write_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    """Durably replace a public artifact without exposing a partially written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_dir():
        raise IsADirectoryError(path)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


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


class PilotService:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.output_dir = reports_directory(runtime)
        self.status_file = worker_status_path(runtime)
        self._cycle_started_at: datetime | None = None
        self._last_success_at = self._previous_last_success()

    def _previous_last_success(self) -> str | None:
        try:
            value = read_worker_status(self.status_file).get("last_success_at")
        except (OSError, ValueError):
            return None
        return str(value) if value else None

    def _write_status(self, state: str, **details: Any) -> None:
        now = datetime.now(UTC).replace(microsecond=0)
        payload: dict[str, Any] = {
            "schema_version": 1,
            "state": state,
            "updated_at": now.isoformat(),
            "pid": os.getpid(),
            "cycle_started_at": self._cycle_started_at.isoformat() if self._cycle_started_at else None,
            "last_success_at": self._last_success_at,
        }
        payload.update(details)
        atomic_write_text(
            self.status_file,
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        )

    def _completed_dates(self, today: date) -> list[date]:
        yesterday = today - timedelta(days=1)
        earliest = self.runtime.db.earliest_sample_time()
        earliest_date = (
            earliest.astimezone(ZoneInfo(self.runtime.config.home.effective_timezone)).date() if earliest else yesterday
        )
        bounded_start = yesterday - timedelta(days=self.runtime.config.pilot.max_catchup_days - 1)
        start = max(min(earliest_date, yesterday), bounded_start)
        return [start + timedelta(days=offset) for offset in range((yesterday - start).days + 1)]

    def _archive_paths(self, report_date: date) -> tuple[Path, Path]:
        archive_dir = self.output_dir / "daily"
        stem = report_date.isoformat()
        return archive_dir / f"{stem}.html", archive_dir / f"{stem}.json"

    def _publish_archive(self, report_date: date, report: Report) -> tuple[Path, Path]:
        html_path, json_path = self._archive_paths(report_date)
        atomic_write_text(
            html_path,
            render_html(
                report,
                self.runtime.db.recommendation_views_for_report(report.id),
                chart_data=cached_chart_data(self.runtime.db, report),
                feedback_api_base_url=self.runtime.config.feedback.public_api_base_url,
                current_comfort_band_c=self.runtime.config.preferences.comfort_band_c,
                latest_report_href="../latest.html",
            ),
            mode=0o644,
        )
        atomic_write_text(json_path, report.model_dump_json(indent=2) + "\n", mode=0o644)
        return html_path, json_path

    def run_cycle(self) -> dict[str, Any]:
        self._cycle_started_at = datetime.now(UTC).replace(microsecond=0)
        self._write_status("starting")
        try:
            from zont_analyzer.application.ai_maintenance import start_review

            try:
                start_review(self.runtime)
            except Exception:
                logger.exception("Could not start model maintenance; telemetry worker continues")
            recommendation_maintenance = self.runtime.maintain_recommendation_lifecycle(
                now=self._cycle_started_at
            )
            self._write_status("syncing")
            with self.runtime.zont_client() as client:
                sync_result = self.runtime.ingestion(client).sync()
            if not sync_result.get("complete", False):
                details = sync_result.get("errors") or "no details"
                raise WorkerCycleError(f"ZONT sync was incomplete: {details}")

            analysis = self.runtime.analysis()
            today = analysis.local_today()
            yesterday = today - timedelta(days=1)
            candidates = self._completed_dates(today)
            analyzed_dates: list[str] = []
            published_dates: list[str] = []
            latest_report: Report | None = None

            for selected in candidates:
                self._write_status(
                    "analyzing",
                    current_date=selected.isoformat(),
                    analyzed_dates=analyzed_dates,
                    published_dates=published_dates,
                    sync=sync_result,
                )
                start, _end = analysis.local_day_window(selected)
                report_id = analysis.report_id_for("daily", start)
                report = self.runtime.db.report(report_id)
                previous_report = report
                html_path, json_path = self._archive_paths(selected)
                data_revision = self.runtime.db.period_data_revision(start, _end)
                # Legacy reports have no revision; unchanged imported history stays intact.
                import hashlib

                empty_revision = hashlib.sha256(b"[]").hexdigest()
                stored_revision = report.context.get("input_revision", {}).get("telemetry") if report else None
                unchanged_import = (
                    report is not None and stored_revision is None and data_revision != empty_revision
                    and self.runtime.db.legacy_period_data_revision(start, _end) == empty_revision
                )
                if (
                    report is not None and isinstance(stored_revision, str)
                    and not stored_revision.startswith("telemetry-v2:")
                    and stored_revision != data_revision
                    and stored_revision == self.runtime.db.legacy_period_data_revision(start, _end)
                ):
                    # A format upgrade is not new evidence. Adopt exact boundaries
                    # only while the legacy markers still match the saved report.
                    if not self.runtime.db.upgrade_report_telemetry_revision(report.id, stored_revision, data_revision):
                        raise WorkerCycleError("Report changed during telemetry revision upgrade; retry next cycle")
                    report = report.model_copy(deep=True)
                    report.context["input_revision"]["telemetry"] = data_revision
                    previous_report = report
                must_analyze = report is None or (
                    selected == yesterday and report.context.get("calculation_version") != CALCULATION_VERSION
                ) or (
                    report is not None and not unchanged_import and (data_revision != empty_revision or (
                        isinstance(stored_revision, str) and stored_revision.startswith("telemetry-v2:")
                    ))
                    and report.context.get("input_revision", {}).get("telemetry") != data_revision
                )
                if must_analyze:
                    # Historic catch-up is deterministic. The existing OpenAI policy is
                    # evaluated only on the first report for yesterday, never on every poll.
                    first_completed_day_report = selected == yesterday and previous_report is None
                    report = self.runtime.analysis().analyze_daily(selected, use_ai=first_completed_day_report)
                    if (
                        previous_report is not None
                        and previous_report.ai_used
                        and previous_report.context.get("recommendation_policy")
                        == report.context.get("recommendation_policy")
                    ):
                        report = reuse_ai_interpretation(previous_report, report)
                        self.runtime.db.save_report(report, render_text(report))
                    analyzed_dates.append(selected.isoformat())
                if report is None:
                    raise WorkerCycleError(f"daily report was not created for {selected.isoformat()}")
                if must_analyze or not html_path.exists() or not json_path.exists():
                    published_dates.append(selected.isoformat())
                if selected == yesterday:
                    latest_report = report

            if latest_report is None:
                raise WorkerCycleError("no report was produced for the latest completed local day")
            from zont_analyzer.application.period_schedule import run_period_schedule

            period_results = run_period_schedule(self.runtime, analysis, today, self.runtime.analysis)
            latest_path = self.output_dir / "latest.html"
            self._write_status(
                "publishing",
                analyzed_dates=analyzed_dates,
                published_dates=published_dates,
                latest_report_id=latest_report.id,
                sync=sync_result,
            )
            from zont_analyzer.application.publication import publish_reports

            publication = publish_reports(self.runtime)
            delivered = self.runtime.db.flush_log_outbox()
            for message in delivered:
                logger.info(message)
            self._last_success_at = datetime.now(UTC).replace(microsecond=0).isoformat()
            result = {
                "ok": True,
                "sync": sync_result,
                "analyzed_dates": analyzed_dates,
                "published_dates": published_dates,
                "latest_report_id": latest_report.id,
                "latest_html": str(latest_path),
                "reports_dir": str(self.output_dir),
                "status_file": str(self.status_file),
                "delivered_log_notifications": len(delivered),
                "recommendation_maintenance": recommendation_maintenance,
                "publication": publication,
                "long_periods": period_results,
            }
            self._write_status("ok", **result)
            return result
        except BaseException as exc:
            self._write_status(
                "error",
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            raise
