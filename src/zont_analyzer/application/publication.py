"""Publish existing reports; this module never runs analysis or calls an external API."""
from __future__ import annotations

import fcntl
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from zont_analyzer.application.pilot import atomic_write_text, reports_directory
from zont_analyzer.domain import Report
from zont_analyzer.reports import render_html

if TYPE_CHECKING:
    from zont_analyzer.runtime import Runtime

KINDS = ("daily", "weekly", "monthly")


def archive_paths(output_dir: Path, report: Report) -> tuple[Path, Path]:
    if report.kind not in KINDS:
        raise ValueError("Only daily, weekly and monthly reports have calendar archives")
    local_start = report.period_start.astimezone(ZoneInfo(report.timezone)).date()
    stem = output_dir / report.kind / local_start.isoformat()
    return stem.with_suffix(".html"), stem.with_suffix(".json")


def _write_changed(path: Path, content: str) -> None:
    if not path.exists() or path.read_text(encoding="utf-8") != content:
        atomic_write_text(path, content, mode=0o644)


def publish_reports(runtime: Runtime, *, now: datetime | None = None) -> dict[str, Any]:
    """Commit complete artifacts before advertising URLs, with serialized writers.

    Each file is fsynced and atomically replaced. The manifest is installed only
    after all its targets exist, and latest is replaced last. A failed publication
    therefore leaves the previous latest intact; no reader sees a partial file.
    The lock also covers feedback republishing in other threads/processes.
    """
    checked_at = now or datetime.now(UTC)
    output_dir = reports_directory(runtime)
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / ".publication.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _publish_locked(runtime, output_dir, checked_at)


def _publish_locked(runtime: Runtime, output_dir: Path, now: datetime) -> dict[str, Any]:
    from zont_analyzer.application.owner_context import OwnerContextStore

    owner_store = OwnerContextStore(runtime.db)
    profiles = [owner_store.profile(str(device["id"])) for device in runtime.db.list_devices()]

    def owner_data(report: Report) -> dict[str, Any]:
        # An old retained export may have no matching DB report; it remains readable.
        gas = None
        if report.kind == "daily" and runtime.db.report(report.id) is not None:
            gas = owner_store.gas(report.id)
        return {"profiles": profiles, "gas": gas}

    # Retain valid existing exports, including dates outside worker catch-up.
    reports: dict[str, Report] = {}
    for kind in KINDS:
        for path in sorted((output_dir / kind).glob("*.json")):
            try:
                report = Report.model_validate_json(path.read_text(encoding="utf-8"))
                html_path, json_path = archive_paths(output_dir, report)
                if json_path != path or not html_path.is_file():
                    continue
                if report.period_end > now or report.generated_at < report.period_end:
                    continue
                reports[str(html_path)] = report
            except (OSError, ValueError):
                continue
    # Newer stored versions replace older exports of the same local period.
    for report in runtime.db.completed_reports(now):
        html_path, _ = archive_paths(output_dir, report)
        previous = reports.get(str(html_path))
        if previous is None or report.generated_at >= previous.generated_at:
            reports[str(html_path)] = report

    entries: list[dict[str, str]] = []
    latest: Report | None = None
    for report in sorted(reports.values(), key=lambda item: (item.kind, item.period_start)):
        html_path, json_path = archive_paths(output_dir, report)
        rendered = render_html(
            report,
            runtime.db.recommendation_views_for_report(report.id),
            feedback_api_base_url=runtime.config.feedback.public_api_base_url,
            latest_report_href="../latest.html",
            owner_data=owner_data(report),
        )
        _write_changed(html_path, rendered)
        _write_changed(json_path, report.model_dump_json(indent=2) + "\n")
        timezone = ZoneInfo(report.timezone)
        entries.append({
            "kind": report.kind,
            "start": report.period_start.astimezone(timezone).date().isoformat(),
            "end": report.period_end.astimezone(timezone).date().isoformat(),
            "href": html_path.relative_to(output_dir).as_posix(),
            "published_at": datetime.fromtimestamp(html_path.stat().st_mtime, UTC).isoformat(),
        })
        if report.kind == "daily" and (latest is None or report.period_start > latest.period_start):
            latest = report

    manifest = {"version": 1, "updated_at": now.astimezone(UTC).isoformat(), "reports": entries}
    atomic_write_text(
        output_dir / "reports.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", mode=0o644
    )
    if latest is not None:
        _write_changed(output_dir / "latest.html", render_html(
            latest,
            runtime.db.recommendation_views_for_report(latest.id),
            feedback_api_base_url=runtime.config.feedback.public_api_base_url,
            owner_data=owner_data(latest),
        ))
    return {
        "reports": len(entries),
        "manifest": str(output_dir / "reports.json"),
        "latest_report_id": latest.id if latest else None,
    }
