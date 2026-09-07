"""Publish reports with current local gas estimates; no AI or external API calls."""
from __future__ import annotations

import fcntl
import html
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from zont_analyzer.application.pilot import atomic_write_text, reports_directory
from zont_analyzer.domain import Report
from zont_analyzer.reports import render_html
from zont_analyzer.reports.chart_data import cached_chart_data

if TYPE_CHECKING:
    from zont_analyzer.runtime import Runtime

KINDS = ("daily", "weekly", "monthly", "seasonal")


def archive_paths(output_dir: Path, report: Report) -> tuple[Path, Path]:
    if report.kind not in KINDS:
        raise ValueError("Only daily, weekly, monthly and seasonal reports have calendar archives")
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


def publish_report(runtime: Runtime, report_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Refresh one already stored report without walking or recalculating the archive."""
    checked_at = now or datetime.now(UTC)
    output_dir = reports_directory(runtime)
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / ".publication.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Read after acquiring the lock so a concurrent regeneration cannot leave
        # an older DB snapshot queued behind its newer publication.
        report = runtime.db.report(report_id)
        if report is None:
            return {"reports": 0, "manifest": str(output_dir / "reports.json"),
                    "latest_report_id": None}
        return _publish_report_locked(runtime, output_dir, report, checked_at)


def _publish_report_locked(
    runtime: Runtime, output_dir: Path, report: Report, now: datetime,
) -> dict[str, Any]:
    if report.kind not in KINDS:
        return {"reports": 0, "manifest": str(output_dir / "reports.json"), "latest_report_id": None}
    if report.period_end > now:
        raise ValueError("Cannot publish a future observation interval")
    if report.generated_at < report.period_end:
        raise ValueError("Cannot publish an incomplete report")

    from zont_analyzer.application.owner_context import OwnerContextStore

    owner_store = OwnerContextStore(runtime.db)
    owner_data: dict[str, Any] = {
        "profiles": [owner_store.profile(str(device["id"])) for device in runtime.db.list_devices()]
    }
    if report.kind == "daily":
        owner_data["gas"] = owner_store.gas(report.id)
    has_other_exports = any(next((output_dir / kind).glob("*.html"), None) for kind in KINDS)
    html_path, json_path = archive_paths(output_dir, report)
    # A regeneration can publish a newer snapshot while this request is waiting
    # for the lock. Preserve that snapshot, but render its current feedback state.
    published_report = report
    if json_path.is_file():
        try:
            candidate = Report.model_validate_json(json_path.read_text(encoding="utf-8"))
            if candidate.generated_at >= report.generated_at and candidate.id != report.id:
                return {"reports": 0, "manifest": str(output_dir / "reports.json"),
                        "latest_report_id": None}
            if candidate.id == report.id and candidate.generated_at > report.generated_at:
                published_report = candidate
        except (OSError, ValueError):
            pass
    rendered = render_html(
        published_report,
        runtime.db.recommendation_views_for_report(published_report.id),
        chart_data=cached_chart_data(runtime.db, published_report),
        feedback_api_base_url=runtime.config.feedback.public_api_base_url,
        latest_report_href="../latest.html",
        owner_data=owner_data,
    )
    _write_changed(html_path, rendered)
    if published_report is report:
        _write_changed(json_path, report.model_dump_json(indent=2) + "\n")

    manifest_path = output_dir / "reports.json"
    manifest_valid = True
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries = manifest["reports"]
        if not isinstance(entries, list):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        manifest_valid = False
        manifest = {"version": 1, "reports": []}
        entries = manifest["reports"]
    report = published_report
    timezone = ZoneInfo(report.timezone)
    entry = {
        "kind": report.kind,
        "start": report.period_start.astimezone(timezone).date().isoformat(),
        "end": report.period_end.astimezone(timezone).date().isoformat(),
        "href": html_path.relative_to(output_dir).as_posix(),
        "published_at": datetime.fromtimestamp(html_path.stat().st_mtime, UTC).isoformat(),
        "timezone": report.timezone,
        "complete": report.context.get("period", {}).get("complete", True),
        "nominal_end": report.context.get("period", {}).get("end", report.period_end.isoformat()),
        "season": report.context.get("period", {}).get("season"),
    }
    # A missing manifest is valid only for a genuinely empty archive (the first
    # report). Never rebuild a missing or malformed manifest over retained files.
    if manifest_valid or not has_other_exports:
        for index, existing in enumerate(entries):
            if isinstance(existing, dict) and existing.get("href") == entry["href"]:
                entries[index] = entry
                break
        else:
            entries.append(entry)
        manifest["updated_at"] = now.astimezone(UTC).isoformat()
        atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", mode=0o644)

    daily_starts: list[str] = []
    latest_path = output_dir / "latest.html"
    latest_matches_report = latest_path.is_file() and (
        f'data-report-id="{html.escape(report.id, quote=True)}"' in latest_path.read_text(encoding="utf-8")
    )
    can_update_latest = manifest_valid or not has_other_exports or latest_matches_report
    if report.kind == "daily" and can_update_latest:
        daily_starts = [
            str(item.get("start"))
            for item in entries
            if isinstance(item, dict) and item.get("kind") == "daily"
        ]
        if not daily_starts or entry["start"] >= max(daily_starts):
            _write_changed(output_dir / "latest.html", render_html(
                published_report,
                runtime.db.recommendation_views_for_report(published_report.id),
                chart_data=cached_chart_data(runtime.db, published_report),
                feedback_api_base_url=runtime.config.feedback.public_api_base_url,
                owner_data=owner_data,
            ))
    latest_id = (
        published_report.id
        if report.kind == "daily" and can_update_latest
        and (not daily_starts or entry["start"] >= max(daily_starts))
        else None
    )
    return {"reports": len(entries), "manifest": str(manifest_path), "latest_report_id": latest_id}


def _publish_locked(
    runtime: Runtime, output_dir: Path, now: datetime, *, overrides: list[Report] | None = None,
) -> dict[str, Any]:
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
    for report in overrides or []:
        if report.period_end > now:
            raise ValueError("Cannot publish a future observation interval")
        html_path, _ = archive_paths(output_dir, report)
        reports[str(html_path)] = report

    from zont_analyzer.application.gas import GasService

    gas_service = GasService(runtime.db, runtime.config)
    candidate_ids = {report.id for report in overrides or []}
    for report_path, report in list(reports.items()):
        refreshed = gas_service.refresh(report)
        # Regeneration owns the candidate commit after publication succeeds.
        if report.id in candidate_ids or gas_service.persist_refresh(report, refreshed):
            reports[report_path] = refreshed
        else:
            # A concurrent regeneration won the optimistic guard. Never publish
            # or store our older AI/context over that revision.
            reports[report_path] = runtime.db.report(report.id) or report

    entries: list[dict[str, Any]] = []
    latest: Report | None = None
    for report in sorted(reports.values(), key=lambda item: (item.kind, item.period_start)):
        html_path, json_path = archive_paths(output_dir, report)
        rendered = render_html(
            report,
            runtime.db.recommendation_views_for_report(report.id),
            chart_data=cached_chart_data(runtime.db, report),
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
            "timezone": report.timezone,
            "complete": report.context.get("period", {}).get("complete", True),
            "nominal_end": report.context.get("period", {}).get("end", report.period_end.isoformat()),
            "season": report.context.get("period", {}).get("season"),
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
            chart_data=cached_chart_data(runtime.db, latest),
            feedback_api_base_url=runtime.config.feedback.public_api_base_url,
            owner_data=owner_data(latest),
        ))
    return {
        "reports": len(entries),
        "manifest": str(output_dir / "reports.json"),
        "latest_report_id": latest.id if latest else None,
    }
