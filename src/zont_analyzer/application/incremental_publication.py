"""Bounded publication from canonical YDB reports and a durable YDB index.

The change checkpoint and dirty flags commit only after all artifacts are
atomically published. A failed run may repeat work but cannot acknowledge an
unpublished change.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from zont_analyzer.adapters.ydb.publication import PublicationRepository
from zont_analyzer.domain import Report

if TYPE_CHECKING:
    from zont_analyzer.cloud.publication import CloudPublication
    from zont_analyzer.runtime import Runtime

VERSION = "publication-v1"
RENDER, GAS, COST = 1, 2, 4
AUDIT_SIZE = 16
LEASE_SECONDS = 240


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _stamp(path: Path) -> str:
    try:
        stat = path.stat()
        return f"{stat.st_mtime_ns}:{stat.st_size}"
    except FileNotFoundError:
        return ""


def _micros(moment: datetime) -> int:
    return int(moment.timestamp() * 1_000_000)


def _windows(report: Report) -> tuple[float, float]:
    """Conservative union of observation, savings and comparison source windows."""
    bounds = [report.period_start.timestamp(), report.period_end.timestamp()]

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for key, first in value.items():
                if key.endswith("start") and isinstance(first, str):
                    last = value.get(key[:-5] + "end")
                    if isinstance(last, str):
                        try:
                            a, b = datetime.fromisoformat(first), datetime.fromisoformat(last)
                            if a.tzinfo and b.tzinfo:
                                bounds.extend((a.timestamp(), b.timestamp()))
                        except ValueError:
                            pass
                if isinstance(first, (dict, list)):
                    visit(first)
    visit(report.context.get("gas_savings"))
    visit(report.context.get("period_comparisons"))
    return min(bounds) - 900, max(bounds) + 900


def _entry(output: Path, report: Report) -> dict[str, Any]:
    from zont_analyzer.application.publication import archive_paths

    html_path, _ = archive_paths(output, report)
    zone = ZoneInfo(report.timezone)
    return {
        "kind": report.kind, "start": report.period_start.astimezone(zone).date().isoformat(),
        "end": report.period_end.astimezone(zone).date().isoformat(),
        "href": html_path.relative_to(output).as_posix(),
        "published_at": datetime.fromtimestamp(html_path.stat().st_mtime, UTC).isoformat(),
        "timezone": report.timezone,
        "complete": report.context.get("period", {}).get("complete", True),
        "nominal_end": report.context.get("period", {}).get("end", report.period_end.isoformat()),
        "season": report.context.get("period", {}).get("season"),
    }


def _enqueue(items: dict[str, dict[str, Any]], flags: int, now: datetime,
             predicate: Callable[[dict[str, Any]], bool] = lambda _: True) -> None:
    for item in items.values():
        if predicate(item):
            if not item["dirty"]:
                item["queued"] = _micros(now)
            item["dirty"] |= flags | RENDER


def _remember(items: dict[str, dict[str, Any]], output: Path, report: Report,
              now: datetime, *, cloud: bool = False) -> bool:
    from zont_analyzer.application.publication import KINDS, archive_paths

    if report.kind not in KINDS or report.period_end > now or report.generated_at < report.period_end:
        return False
    html_path, json_path = archive_paths(output, report)
    href = html_path.relative_to(output).as_posix()
    previous = items.get(href)
    digest = _digest(report.model_dump(mode="json"))
    if previous and (previous["generated"] > _micros(report.generated_at) or previous["digest"] == digest):
        return False
    lo, hi = _windows(report)
    items[href] = {
        "href": href, "report_id": report.id, "kind": report.kind,
        "start": int(report.period_start.timestamp()), "end": int(report.period_end.timestamp()),
        "generated": _micros(report.generated_at), "digest": digest, "lo": lo, "hi": hi,
        "comparisons": bool(report.context.get("period_comparisons")),
        "dirty": (previous["dirty"] if previous else 0) | RENDER | GAS,
        "queued": previous["queued"] if previous and previous["dirty"] else _micros(now),
        "entry": previous["entry"] if previous else None,
        "json_stamp": previous["json_stamp"] if previous else ("" if cloud else _stamp(json_path)),
        "html_stamp": previous["html_stamp"] if previous else ("" if cloud else _stamp(html_path)),
    }
    return True


def _dependents(items: dict[str, dict[str, Any]], report: Report, now: datetime) -> None:
    end, start = report.period_end.timestamp(), report.period_start.timestamp()
    _enqueue(items, COST, now, lambda item: item["comparisons"] and item["report_id"] != report.id
             and item["lo"] < end and item["hi"] > start)


def _telemetry(items: dict[str, dict[str, Any]], repository: PublicationRepository,
               runtime: Runtime, hours: list[str], now: datetime) -> None:
    if not hours:
        return
    earliest, latest = repository.reading_span()
    calibration: tuple[float, float] | None = None
    if earliest and latest and earliest != latest:
        zone = ZoneInfo(runtime.config.home.effective_timezone)
        calibration = (
            datetime.fromisoformat(earliest).replace(tzinfo=zone).timestamp() - 900,
            (datetime.fromisoformat(latest).replace(tzinfo=zone) + timedelta(days=1)).timestamp() + 900,
        )
    for hour in hours:
        start = datetime.strptime(hour, "%Y-%m-%dT%H").replace(tzinfo=UTC).timestamp()
        end = start + 3600
        if calibration and start < calibration[1] and end > calibration[0]:
            _enqueue(items, GAS, now)
            return
        _enqueue(items, GAS, now, lambda item: item["lo"] < end and item["hi"] > start)  # noqa: B023


def _queue(items: dict[str, dict[str, Any]], latest_href: str, batch_size: int) -> list[dict[str, Any]]:
    return sorted((item for item in items.values() if item["dirty"]), key=lambda item: (
        0 if item["href"] == latest_href else 1 if item["entry"] is None else 2,
        -item["start"] if item["entry"] is None else 0,
        item["queued"], item["href"],
    ))[:batch_size]


def publish_incremental(runtime: Runtime, output: Path, now: datetime, *,
                        batch_size: int = 8, rebuild: bool = False,
                        cloud: CloudPublication | None = None) -> dict[str, Any]:
    if not 1 <= batch_size <= 100:
        raise ValueError("publication batch_size must be between 1 and 100")
    if cloud is not None:
        batch_size = min(batch_size, 8)
    repository = PublicationRepository(runtime.db.storage)
    owner = str(uuid4())
    lease = runtime.db.jobs.acquire("publication", owner, LEASE_SECONDS)
    if lease is None:
        raise RuntimeError("another publisher holds the YDB publication lease")
    try:
        return _run(repository, runtime, output, now, batch_size, rebuild, owner, lease.attempt, cloud)
    finally:
        runtime.db.jobs.release("publication", owner, lease.attempt)


def _run(repository: PublicationRepository, runtime: Runtime, output: Path, now: datetime,
         batch_size: int, rebuild: bool, owner: str, attempt: int,
         cloud: CloudPublication | None = None) -> dict[str, Any]:
    from zont_analyzer.application import publication as pub
    from zont_analyzer.application.ai_maintenance import review_state
    from zont_analyzer.application.gas import GasService
    from zont_analyzer.application.gas_tariffs import GasTariffStore
    from zont_analyzer.application.owner_context import OwnerContextStore
    from zont_analyzer.application.timezone import apply_device_timezone

    def fence() -> None:
        if runtime.db.jobs.renew("publication", owner, attempt, LEASE_SECONDS) is None:
            raise RuntimeError("publisher lost its YDB lease")

    apply_device_timezone(runtime.db, runtime.config)
    owner_store = OwnerContextStore(runtime.db)
    profiles = [owner_store.profile(str(device["id"]), now) for device in runtime.db.list_devices()]
    profile_digest = _digest([{key: value for key, value in profile.items() if key != "as_of"} for profile in profiles])
    ai_review = review_state(runtime)
    config_values = (VERSION, runtime.config.home.model_dump(), runtime.config.home.effective_timezone,
                     runtime.config.preferences.model_dump(), runtime.config.analysis.model_dump(),
                     runtime.config.feedback.public_api_base_url)
    storage_target = _digest((cloud.storage.bucket, cloud.storage.prefix)) if cloud else ""
    config_digest = _digest(config_values + (storage_target,) if cloud else config_values)
    review_digest = _digest(ai_review)
    meta_hint = repository.load_meta()
    checkpoint = int(meta_hint.get("checkpoint", "0"))
    upper = runtime.db.source_revision()
    manifest_path = output / "reports.json"
    manifest_current = (bool(meta_hint.get("manifest_key")) and
                        bool(cloud.storage.head(meta_hint["manifest_key"]))) if cloud else (
                            meta_hint.get("manifest_stamp") == _stamp(manifest_path) and manifest_path.exists())
    if (
        not rebuild and upper == checkpoint and meta_hint.get("identity") == runtime.db.identity
        and meta_hint.get("profiles") == profile_digest and meta_hint.get("config") == config_digest
        and meta_hint.get("ai_review") == review_digest and "count" in meta_hint
        and manifest_current
        and not repository.has_dirty()
    ):
        latest_hint = repository.latest_daily()
        latest_current = (bool(meta_hint.get("latest_key")) and
                          bool(cloud.storage.head(meta_hint["latest_key"]))) if cloud else (
                              meta_hint.get("latest_stamp") == _stamp(output / "latest.html"))
        if latest_hint is None or latest_current:
            audit_hint = repository.audit_page(meta_hint.get("audit", ""), AUDIT_SIZE)
            if all(
                (bool(row["entry"]) and cloud.valid_entry(
                    json.loads(row["entry"]), row["html_stamp"], row["json_stamp"])) if cloud else
                (row["json_stamp"] == _stamp((output / row["href"]).with_suffix(".json"))
                 and row["html_stamp"] == _stamp(output / row["href"]))
                for row in audit_hint
            ):
                updated_meta = dict(meta_hint)
                updated_meta["audit"] = audit_hint[-1]["href"] if len(audit_hint) == AUDIT_SIZE else ""
                fence()
                if not repository.save({}, {}, updated_meta, meta_hint, expected_checkpoint=checkpoint,
                                       lease_owner=owner, lease_attempt=attempt):
                    raise RuntimeError("publisher lost its YDB checkpoint or lease")
                return {"reports": int(meta_hint["count"]), "rendered_reports": 0, "pending_reports": 0,
                        "manifest": meta_hint["manifest_key"] if cloud else str(manifest_path),
                        "latest_report_id": latest_hint["report_id"] if latest_hint else None}

    items, meta = repository.load()
    previous, old_meta = deepcopy(items), dict(meta)
    checkpoint = int(meta.get("checkpoint", "0"))
    upper = runtime.db.source_revision()
    changes = repository.changes_since(checkpoint, upper)
    identity = runtime.db.identity
    recovering = (rebuild or upper < checkpoint or meta.get("identity") != identity
                  or (cloud is not None and
                      (not meta.get("manifest_key") or meta.get("storage_target") != storage_target)))
    if recovering:
        items.clear()
        for canonical_report in repository.canonical_reports(now):
            _remember(items, output, canonical_report, now, cloud=cloud is not None)
        meta["identity"] = identity
    else:
        for change in changes:
            scope, identifier = str(change["scope"]), str(change["identifier"])
            if scope == "report":
                report = runtime.db.report(identifier)
                if report is not None and _remember(items, output, report, now, cloud=cloud is not None):
                    _dependents(items, report, now)
            elif scope == "render":
                _enqueue(items, RENDER, now, lambda item: item["report_id"] == identifier)  # noqa: B023
            elif scope == "global":
                _enqueue(items, {"gas": GAS, "cost": COST}.get(identifier, RENDER), now)
            elif scope == "tariff" and identifier:
                start = datetime.fromisoformat(identifier.replace("Z", "+00:00")).timestamp()
                following = repository.next_tariff_start(identifier)
                end = (datetime.fromisoformat(following.replace("Z", "+00:00")).timestamp()
                       if following else float("inf"))
                _enqueue(items, COST, now,
                         lambda item: item["lo"] + 900 < end and item["hi"] - 900 > start)  # noqa: B023
            elif scope.startswith("tariff:"):
                _enqueue(items, COST, now)
            elif scope.startswith(("owner-profile:", "owner-gas:", "device:", "series:", "telemetry:")):
                _enqueue(items, GAS, now)
        _telemetry(items, repository, runtime,
                   [str(c["identifier"]) for c in changes if c["scope"] == "telemetry"], now)

    # A lost local publication directory is reconciled from canonical YDB.
    # Ordinary polls examine only sixteen paths and do no full archive walk.
    if cloud is None and not manifest_path.exists():
        for item in items.values():
            if not (output / item["href"]).is_file() or not (output / item["href"]).with_suffix(".json").is_file():
                _enqueue(items, GAS, now, lambda row: row["href"] == item["href"])  # noqa: B023
                item["entry"] = None
    if meta.get("profiles") != profile_digest:
        _enqueue(items, GAS, now)
    meta["profiles"] = profile_digest
    if meta.get("config") != config_digest:
        _enqueue(items, GAS, now)
    meta["config"] = config_digest
    if meta.get("ai_review") != review_digest:
        _enqueue(items, RENDER, now)
    meta["ai_review"] = review_digest

    hrefs = sorted(items)
    cursor = meta.get("audit", "")
    later = [href for href in hrefs if href > cursor]
    audit = later[:AUDIT_SIZE]
    for href in audit:
        item = items[href]
        path = output / href
        if cloud:
            damaged = not item["entry"] or not cloud.valid_entry(
                json.loads(item["entry"]), item["html_stamp"], item["json_stamp"])
        else:
            damaged = (item["json_stamp"] != _stamp(path.with_suffix(".json"))
                       or item["html_stamp"] != _stamp(path))
        if damaged:
            _enqueue(items, GAS, now, lambda row: row["href"] == href)  # noqa: B023
    meta["audit"] = audit[-1] if len(audit) == AUDIT_SIZE else ""

    latest = max((item for item in items.values() if item["kind"] == "daily"),
                 key=lambda item: (item["start"], item["href"]), default=None)
    latest_href = latest["href"] if latest else ""
    latest_missing = (not meta.get("latest_key") or not cloud.storage.head(meta["latest_key"])) if cloud else (
        meta.get("latest_stamp") != _stamp(output / "latest.html"))
    if latest and latest_missing:
        _enqueue(items, RENDER, now, lambda item: item["href"] == latest_href)
    queue = _queue(items, latest_href, batch_size)
    service = GasService(runtime.db, runtime.config) if any(item["dirty"] & (GAS | COST) for item in queue) else None
    tariffs = GasTariffStore(runtime.db).history() if queue else []

    def render(report: Report, *, is_latest: bool = False) -> str:
        owner_data = {
            "profiles": profiles, "tariffs": tariffs, "ai_review": ai_review,
            "gas": owner_store.gas(report.id) if report.kind == "daily" and runtime.db.report(report.id) else None,
        }
        return pub.render_html(report, runtime.db.recommendation_views_for_report(report.id),
                               chart_data=pub.cached_chart_data(runtime.db, report),
                               feedback_api_base_url=runtime.config.feedback.public_api_base_url,
                               current_comfort_band_c=runtime.config.preferences.comfort_band_c,
                               latest_report_href="latest.html" if is_latest else "../latest.html",
                               owner_data=owner_data)

    rendered = 0
    newest: Report | None = None
    for item in queue:
        fence()
        report = runtime.db.report(item["report_id"])
        if report is None:
            items.pop(item["href"], None)
            continue
        if service is not None and item["dirty"] & (GAS | COST):
            refreshed = service.refresh(report) if item["dirty"] & GAS else service.refresh_cost(report)
            if service.persist_refresh(report, refreshed):
                if item["dirty"] & GAS and report.context.get("gas") != refreshed.context.get("gas"):
                    _dependents(items, refreshed, now)
                report = refreshed
            else:
                report = runtime.db.report(report.id) or report
        html_path, json_path = pub.archive_paths(output, report)
        html = render(report)
        fence()
        if cloud:
            entry, html_stamp, json_stamp = cloud.report(report, item["href"], html, now)
        else:
            pub._write_changed(html_path, html)
            pub._write_changed(json_path, report.model_dump_json(indent=2) + "\n")
            entry, html_stamp, json_stamp = _entry(output, report), _stamp(html_path), _stamp(json_path)
        lo, hi = _windows(report)
        item.update(dirty=0, entry=json.dumps(entry),
                    digest=_digest(report.model_dump(mode="json")), lo=lo, hi=hi,
                    generated=_micros(report.generated_at), json_stamp=json_stamp, html_stamp=html_stamp)
        if item["href"] == latest_href:
            newest = report
        rendered += 1

    fence()
    entries = [json.loads(item["entry"]) for item in sorted(items.values(), key=lambda row: (row["kind"], row["start"]))
               if item["entry"] is not None]
    if cloud:
        meta["storage_target"] = storage_target
        if latest is None:
            meta["latest_key"] = ""
        if queue or recovering or not meta.get("manifest_key") or not cloud.storage.head(meta["manifest_key"]):
            meta["manifest_key"] = cloud.manifest(entries, now)
        if newest is not None:
            fence()
            meta["latest_key"] = cloud.latest(render(newest, is_latest=True))
    else:
        if queue or recovering or meta.get("manifest_stamp") != _stamp(manifest_path) or not manifest_path.exists():
            manifest = {"version": 1, "updated_at": now.astimezone(UTC).isoformat(), "reports": entries}
            pub.atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", mode=0o644)
        if newest is not None:
            fence()
            pub._write_changed(output / "latest.html", render(newest, is_latest=True))
        meta["manifest_stamp"] = _stamp(manifest_path)
        meta["latest_stamp"] = _stamp(output / "latest.html")
    meta["checkpoint"] = str(upper)
    meta["count"] = str(len(entries))
    meta["latest_report_id"] = latest["report_id"] if latest else ""
    fence()
    if not repository.save(items, previous, meta, old_meta, expected_checkpoint=checkpoint,
                           lease_owner=owner, lease_attempt=attempt):
        raise RuntimeError("publisher lost its YDB checkpoint or lease")
    return {"reports": len(entries), "rendered_reports": rendered,
            "pending_reports": sum(bool(item["dirty"]) for item in items.values()),
            "manifest": meta["manifest_key"] if cloud else str(manifest_path),
            "latest_report_id": latest["report_id"] if latest else None}
