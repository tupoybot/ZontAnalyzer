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
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from zont_analyzer.adapters.ydb.publication import PublicationRepository, queue_metrics
from zont_analyzer.domain import Report

if TYPE_CHECKING:
    from zont_analyzer.cloud.publication import CloudPublication
    from zont_analyzer.runtime import Runtime

VERSION = "publication-v2"
DIRTY_SEMANTICS = "regulation-v1"
# Value 2 was the gas recalculation flag. Existing queue items also have
# RENDER and can be drained normally without assigning a new meaning to 2.
RENDER, COST, SETTINGS, REGULATION = 1, 4, 8, 16
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


def _unpublished_basis(meta: dict[str, str]) -> str:
    return json.dumps([meta.get("checkpoint", "0"), meta.get("count", "0")], separators=(",", ":"))


def _cloud_object_current(cloud: CloudPublication, meta: dict[str, str], name: str) -> bool:
    key = meta.get(f"{name}_key", "")
    if not key:
        return False
    etag = cloud.storage.head(key)
    expected = meta.get(f"{name}_etag")
    return bool(etag) and (not expected or etag == expected)


def _dependency_windows(report: Report, keys: tuple[str, ...]) -> tuple[float, float]:
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
    for key in keys:
        visit(report.context.get(key))
    return min(bounds) - 900, max(bounds) + 900


def _gas_windows(report: Report) -> tuple[float, float]:
    return _dependency_windows(report, ("gas_savings", "period_comparisons"))


def _regulation_windows(report: Report) -> tuple[float, float] | None:
    from .regulation_refresh import REGULATION_KEYS, regulation_context_scopes

    return _dependency_windows(report, REGULATION_KEYS) if regulation_context_scopes(report.context) else None


def _windows(report: Report) -> tuple[float, float]:
    """Candidate index union; each invalidation still checks its own dependency."""
    tariff, regulation = _gas_windows(report), _regulation_windows(report)
    return (min(tariff[0], regulation[0]), max(tariff[1], regulation[1])) if regulation else tariff


def _has_dependencies(report: Report) -> bool:
    return _regulation_windows(report) is not None


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
        "comparisons": _has_dependencies(report),
        "dirty": (previous["dirty"] if previous else 0) | RENDER | REGULATION,
        "queued": previous["queued"] if previous and previous["dirty"] else _micros(now),
        "entry": previous["entry"] if previous else None,
        "json_stamp": previous["json_stamp"] if previous else ("" if cloud else _stamp(json_path)),
        "html_stamp": previous["html_stamp"] if previous else ("" if cloud else _stamp(html_path)),
    }
    return True


def _dependents(items: dict[str, dict[str, Any]], report: Report, now: datetime, *, db: Any) -> None:
    from .regulation_refresh import regulation_context_scopes, report_depends_on_regulation_scope

    end, start = report.period_end.timestamp(), report.period_start.timestamp()
    scopes = regulation_context_scopes(report.context)
    for item in items.values():
        if not (item["comparisons"] and item["report_id"] != report.id and item["lo"] < end and item["hi"] > start):
            continue
        candidate = db.report(item["report_id"])
        if candidate is None:
            continue
        regulation = _regulation_windows(candidate)
        if (regulation and regulation[0] < end and regulation[1] > start
                and (not scopes or any(report_depends_on_regulation_scope(candidate, scope) for scope in scopes))):
            _enqueue({item["href"]: item}, REGULATION, now)


def _telemetry(items: dict[str, dict[str, Any]], hours: list[str], now: datetime, *, db: Any) -> None:
    for hour in hours:
        start = datetime.strptime(hour, "%Y-%m-%dT%H").replace(tzinfo=UTC).timestamp()
        end = start + 3600
        for item in items.values():
            if not (item["lo"] < end and item["hi"] > start):
                continue
            flags = RENDER if item["start"] - 900 < end and item["end"] + 900 > start else 0
            if item["comparisons"] and (report := db.report(item["report_id"])):
                regulation = _regulation_windows(report)
                if regulation and regulation[0] < end and regulation[1] > start:
                    flags |= REGULATION
            if flags:
                _enqueue({item["href"]: item}, flags, now)


def _regulation_change(items: dict[str, dict[str, Any]], change: dict[str, Any],
                       db: Any, now: datetime) -> None:
    """Queue existing dependent index items; never enumerate canonical reports."""
    from .regulation_refresh import report_depends_on_regulation_scope

    payload = change.get("payload", {})
    if isinstance(payload, str):
        payload = json.loads(payload)
    first, last = payload.get("change_start"), payload.get("change_end")
    bounded = type(first) is int and type(last) is int and first <= last
    left, right = (first / 1_000_000, last / 1_000_000) if bounded else (float("-inf"), float("inf"))
    scope = str(change["identifier"])
    dependent = {
        item["report_id"] for item in items.values()
        if item["lo"] <= right and item["hi"] > left
        and (report := db.report(item["report_id"])) is not None
        and report_depends_on_regulation_scope(report, scope)
        and (bounds := _regulation_windows(report)) is not None and bounds[0] <= right and bounds[1] > left
    }
    _enqueue(items, REGULATION, now, lambda item: item["report_id"] in dependent)


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
        result = _run(repository, runtime, output, now, batch_size, rebuild, owner, lease.attempt, cloud)
        if cloud is not None:
            # The index is a repairable projection: only publish freshly read,
            # committed pointers after the fenced save, including on idle retries.
            cloud.update_site_index(repository)
        return result
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
    from zont_analyzer.reports.gas_validation import VIEW_VERSION as gas_validation_view
    from zont_analyzer.reports.renderers import VIEW_VERSION as report_view

    def fence() -> None:
        if runtime.db.jobs.renew("publication", owner, attempt, LEASE_SECONDS) is None:
            raise RuntimeError("publisher lost its YDB lease")

    meta_hint = repository.load_meta()
    storage_target = cloud.storage_target if cloud else ""
    if (cloud and meta_hint.get("storage_target") == storage_target
            and meta_hint.get("manifest_key", "").startswith("publication/")):
        # The separate migration copies the exact published bytes. Do not let
        # an ordinary release replace the archive with freshly rendered data.
        raise RuntimeError("published archive requires the separate stable-path migration")

    apply_device_timezone(runtime.db, runtime.config)
    owner_store = OwnerContextStore(runtime.db)
    profiles = [owner_store.profile(str(device["id"]), now) for device in runtime.db.list_devices()]
    profile_digest = _digest([{key: value for key, value in profile.items() if key != "as_of"} for profile in profiles])
    ai_review = review_state(runtime)
    config_values = (VERSION, runtime.config.home.model_dump(), runtime.config.home.effective_timezone,
                     runtime.config.preferences.model_dump(), runtime.config.analysis.model_dump(),
                     runtime.config.feedback.public_api_base_url)
    config_digest = _digest(config_values + (storage_target,) if cloud else config_values)
    review_digest = _digest(ai_review)
    checkpoint = int(meta_hint.get("checkpoint", "0"))
    upper = runtime.db.source_revision()
    manifest_path = output / "reports.json"
    manifest_current = _cloud_object_current(cloud, meta_hint, "manifest") if cloud else (
                            meta_hint.get("manifest_stamp") == _stamp(manifest_path) and manifest_path.exists())
    if (
        not rebuild and upper == checkpoint and meta_hint.get("identity") == runtime.db.identity
        and meta_hint.get("profiles") == profile_digest and meta_hint.get("config") == config_digest
        and meta_hint.get("ai_review") == review_digest and "count" in meta_hint
        and meta_hint.get("gas_validation_view") == gas_validation_view
        and meta_hint.get("report_view") == report_view
        and meta_hint.get("dirty_semantics") == DIRTY_SEMANTICS
        and "unpublished_count" in meta_hint
        and meta_hint.get("unpublished_basis") == _unpublished_basis(meta_hint)
        and manifest_current
        and not repository.has_dirty()
    ):
        latest_hint = repository.latest_daily()
        latest_current = _cloud_object_current(cloud, meta_hint, "latest") if cloud else (
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
                        "queue_metrics": queue_metrics(updated_meta),
                        "pending_oldest_timestamp_seconds": None,
                        "manifest": meta_hint["manifest_key"] if cloud else str(manifest_path),
                        "latest_report_id": latest_hint["report_id"] if latest_hint else None}

    changes_hint = repository.changes_since(checkpoint, upper) if upper > checkpoint else []
    hours = sorted({str(change["identifier"]) for change in changes_hint if change["scope"] == "telemetry"})
    meter_days = {str(change["identifier"]) for change in changes_hint
                  if str(change["scope"]).startswith("owner-gas:")}
    form_hrefs = {f"daily/{day}.html" for day in meter_days if day}
    scoped_changes = bool(changes_hint) and len(hours) <= 16 and all(
        change["scope"] in {"telemetry", "telemetry-gas"}
        or str(change["scope"]).startswith("owner-gas:")
        or (change["scope"], change["identifier"]) == ("global", "gas")
        for change in changes_hint
    )

    # A stable backlog needs only one bounded queue batch. Broad invalidations
    # and recovery still load the index so their dirty flags remain durable.
    partial = (
        not rebuild and (upper == checkpoint or scoped_changes)
        and meta_hint.get("identity") == runtime.db.identity
        and meta_hint.get("profiles") == profile_digest and meta_hint.get("config") == config_digest
        and meta_hint.get("ai_review") == review_digest and "count" in meta_hint
        and meta_hint.get("gas_validation_view") == gas_validation_view
        and meta_hint.get("report_view") == report_view
        and meta_hint.get("dirty_semantics") == DIRTY_SEMANTICS
        and "unpublished_count" in meta_hint
        and meta_hint.get("unpublished_basis") == _unpublished_basis(meta_hint)
        and manifest_current and (scoped_changes or repository.has_dirty())
    )
    latest_hint = repository.latest_daily() if partial else None
    if partial:
        latest_current = _cloud_object_current(cloud, meta_hint, "latest") if cloud else (
                              meta_hint.get("latest_stamp") == _stamp(output / "latest.html"))
        partial = latest_hint is None or latest_current
    items, meta = ((repository.pending_items(
                        batch_size, str(latest_hint["href"]) if latest_hint else "",
                        include_unpublished=int(meta_hint["unpublished_count"]) > 0),
                    dict(meta_hint)) if partial else repository.load())
    if partial:
        if latest_hint and meter_days:
            form_hrefs.add(str(latest_hint["href"]))
        for href, affected in (repository.affected_items(hours) | repository.items_by_href(form_hrefs)).items():
            items.setdefault(href, affected)
    previous, old_meta = deepcopy(items), dict(meta)
    if meta.get("dirty_semantics") != DIRTY_SEMANTICS:
        # Older pending RENDER flags could represent either view changes or
        # regulation changes. Preserve that unknown work once, before current
        # view invalidations are queued; clean archive items need no migration.
        _enqueue(items, REGULATION, now, lambda item: bool(item["dirty"]))
        meta["dirty_semantics"] = DIRTY_SEMANTICS
    checkpoint = int(meta.get("checkpoint", "0"))
    # Keep the captured high-water mark. A source write during rendering must
    # remain newer than this checkpoint and be observed by the next run.
    changes = changes_hint if not partial else []
    identity = runtime.db.identity
    recovering = (rebuild or upper < checkpoint or meta.get("identity") != identity
                  or (cloud is not None and
                      (not meta.get("manifest_key") or meta.get("storage_target") != storage_target)))
    if recovering and not partial:
        items.clear()
        for canonical_report in repository.canonical_reports(now):
            _remember(items, output, canonical_report, now, cloud=cloud is not None)
        if meta.get("identity") == identity:
            for href, item in items.items():
                prior = previous.get(href)
                if prior and prior["dirty"] and prior["report_id"] == item["report_id"]:
                    item["dirty"] |= prior["dirty"] & (COST | SETTINGS | REGULATION)
                    item["queued"] = prior["queued"]
        meta["identity"] = identity
    if not partial:
        for change in changes:
            scope, identifier = str(change["scope"]), str(change["identifier"])
            if scope == "report" and not recovering:
                report = runtime.db.report(identifier)
                if report is not None and _remember(items, output, report, now, cloud=cloud is not None):
                    _dependents(items, report, now, db=runtime.db)
            elif scope == "render":
                _enqueue(items, RENDER, now, lambda item: item["report_id"] == identifier)  # noqa: B023
            elif scope in {"regulation-facts", "regulation-manual"}:
                _regulation_change(items, change, runtime.db, now)
            elif scope == "control-settings":
                from .settings_history import report_depends_on_device

                bounds = change.get("payload", {})
                if isinstance(bounds, str):
                    bounds = json.loads(bounds)
                left = bounds.get("change_start", float("-inf")) / 1_000_000
                right = bounds.get("change_end", float("inf")) / 1_000_000
                dependent_ids = {
                    item["report_id"] for item in items.values()
                    if item["start"] <= right and item["end"] > left
                    and (candidate := runtime.db.report(item["report_id"])) is not None
                    and report_depends_on_device(candidate, identifier, bounds.get("circuit_ids"))
                }
                _enqueue(items, SETTINGS | REGULATION, now,
                         lambda item: item["report_id"] in dependent_ids)  # noqa: B023
                # A setting can also be the baseline of a later comparison.
                # Its own report period need not overlap the changed boundary.
                for circuit in bounds.get("circuit_ids") or ["*"]:
                    _regulation_change(items, {**change, "identifier": f"{identifier}/{circuit}"}, runtime.db, now)
            elif scope == "global" and identifier != "gas":
                _enqueue(items, COST if identifier == "cost" else RENDER, now)
            elif scope == "tariff" and identifier:
                start = datetime.fromisoformat(identifier.replace("Z", "+00:00")).timestamp()
                following = repository.next_tariff_start(identifier)
                end = (datetime.fromisoformat(following.replace("Z", "+00:00")).timestamp()
                       if following else float("inf"))
                _enqueue(items, COST, now,
                         lambda item: item["lo"] + 900 < end and item["hi"] - 900 > start)  # noqa: B023
            elif scope.startswith("tariff:"):
                _enqueue(items, COST, now)
            elif scope.startswith(("owner-profile:", "series:")):
                _enqueue(items, REGULATION, now)
            elif scope.startswith("telemetry:"):
                # A legacy per-device revision has no hour to target. Modern
                # hourly publication markers use scope="telemetry" below.
                _enqueue(items, REGULATION, now)
            # Raw device snapshots also change on ordinary discovery (timestamps,
            # online state). Equipment facts and device membership are covered by
            # the profile digest below; effective timezone is in the config digest.
    if not recovering:
        _telemetry(items, hours, now, db=runtime.db)

    # A lost local publication directory is reconciled from canonical YDB.
    # Ordinary polls examine only sixteen paths and do no full archive walk.
    if not partial and cloud is None and not manifest_path.exists():
        for item in items.values():
            if not (output / item["href"]).is_file() or not (output / item["href"]).with_suffix(".json").is_file():
                _enqueue({item["href"]: item}, RENDER, now)
                item["entry"] = None
    if not partial and meta.get("profiles") != profile_digest:
        _enqueue(items, REGULATION, now)
    meta["profiles"] = profile_digest
    if not partial and meta.get("config") != config_digest:
        _enqueue(items, REGULATION, now)
    meta["config"] = config_digest
    if not partial and meta.get("ai_review") != review_digest:
        _enqueue(items, RENDER, now)
    meta["ai_review"] = review_digest
    if not partial and (meta.get("gas_validation_view") != gas_validation_view
                        or meta.get("report_view") != report_view):
        # Existing models already contain the validation evidence. Republish
        # the view through bounded batches without recalibrating the archive.
        _enqueue(items, RENDER, now)
    meta["gas_validation_view"] = gas_validation_view
    meta["report_view"] = report_view

    if not partial:
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
                _enqueue({href: item}, RENDER, now)
        meta["audit"] = audit[-1] if len(audit) == AUDIT_SIZE else ""

    latest = (latest_hint if partial else
              max((item for item in items.values() if item["kind"] == "daily"),
                  key=lambda item: (item["start"], item["href"]), default=None))
    latest_href = latest["href"] if latest else ""
    if latest_href and meter_days:
        form_hrefs.add(latest_href)
    if form_hrefs:
        _enqueue(items, RENDER, now, lambda item: item["href"] in form_hrefs)
    latest_missing = not _cloud_object_current(cloud, meta, "latest") if cloud else (
        meta.get("latest_stamp") != _stamp(output / "latest.html"))
    if latest and latest_missing:
        _enqueue(items, RENDER, now, lambda item: item["href"] == latest_href)
    if partial and not any(item["dirty"] for item in items.values()):
        pending, _ = repository.pending_count()
        if pending == 0:
            meta["checkpoint"] = str(upper)
            meta["unpublished_basis"] = _unpublished_basis(meta)
            fence()
            if not repository.save({}, {}, meta, old_meta, expected_checkpoint=checkpoint,
                                   lease_owner=owner, lease_attempt=attempt):
                raise RuntimeError("publisher lost its YDB checkpoint or lease")
            return {"reports": int(meta["count"]), "rendered_reports": 0, "pending_reports": 0,
                    "queue_metrics": queue_metrics(meta),
                    "pending_oldest_timestamp_seconds": None,
                    "manifest": meta["manifest_key"] if cloud else str(manifest_path),
                    "latest_report_id": latest["report_id"] if latest else None}
    # Capture before rendering: a complete enqueue/render cycle can leave the
    # final dirty flag unchanged. Already pending work merely merges flags.
    added = {href for href, item in items.items() if item["dirty"] and href not in previous}
    requeued = {href: 1 for href, item in items.items()
                if item["dirty"] and href in previous and not previous[href]["dirty"]}
    completed: set[str] = set()
    queue = _queue(items, latest_href, batch_size)
    service = GasService(runtime.db, runtime.config) if any(item["dirty"] & COST for item in queue) else None
    tariffs = GasTariffStore(runtime.db).history() if queue else []

    def render(report: Report, *, is_latest: bool = False) -> str:
        owner_data = {
            "profiles": profiles, "tariffs": tariffs, "ai_review": ai_review,
            "gas": owner_store.gas_for_report(report) if report.kind == "daily" else None,
        }
        return pub.render_html(report, runtime.db.recommendation_views_for_report(report.id),
                               chart_data=pub.cached_chart_data(runtime.db, report),
                               feedback_api_base_url=runtime.config.feedback.public_api_base_url,
                               current_comfort_band_c=runtime.config.preferences.comfort_band_c,
                               latest_report_href="latest.html" if is_latest else "../latest.html",
                               owner_data=owner_data)

    rendered = 0
    newest: Report | None = None
    with owner_store.gas_snapshot():
        for item in queue:
            fence()
            report = runtime.db.report(item["report_id"])
            if report is None:
                items.pop(item["href"], None)
                added.discard(item["href"])
                requeued.pop(item["href"], None)
                continue
            if item["dirty"] & SETTINGS:
                from .settings_history import refresh_settings_history

                refreshed = refresh_settings_history(runtime.db, report)
                if runtime.db.reports.replace_context(
                    report, refreshed, f"settings-report-revision:{report.id}",
                    json.dumps(report.context.get("control_settings_history"), sort_keys=True),
                ):
                    report = refreshed
                else:
                    raise RuntimeError("report changed during settings refresh; retry publication")
            if service is not None and item["dirty"] & COST:
                refreshed = service.refresh_cost(report)
                if service.persist_refresh(report, refreshed):
                    report = refreshed
                else:
                    raise RuntimeError("report changed during tariff refresh; retry publication")
            from .regulation_refresh import refresh_regulation_context, regulation_context_fingerprint

            # View/template changes use the saved evidence. Only source
            # invalidations may reread the bounded regulation history. SETTINGS
            # also covers settings work queued before REGULATION was introduced.
            refreshed = refresh_regulation_context(runtime.db, report) if item["dirty"] & (
                REGULATION | SETTINGS
            ) else report
            if refreshed is not report:
                if not runtime.db.reports.replace_context(
                    report, refreshed, f"regulation-report-revision:{report.id}",
                    regulation_context_fingerprint(report.context),
                ):
                    raise RuntimeError("report changed during regulation refresh; retry publication")
                report = refreshed
            html_path, json_path = pub.archive_paths(output, report)
            html = render(report)
            fence()
            if cloud:
                old_entry = json.loads(item["entry"]) if item["entry"] is not None else None
                old_valid = bool(old_entry and cloud.valid_entry(
                    old_entry, item["html_stamp"], item["json_stamp"]))
                entry, html_stamp, json_stamp = cloud.report(report, item["href"], html, now, fence=fence)
                if old_entry is not None and old_valid and item["html_stamp"] == html_stamp \
                        and item["json_stamp"] == json_stamp:
                    entry = old_entry
            else:
                pub._write_changed(html_path, html)
                pub._write_changed(json_path, report.model_dump_json(indent=2) + "\n")
                entry, html_stamp, json_stamp = _entry(output, report), _stamp(html_path), _stamp(json_path)
            lo, hi = _windows(report)
            item.update(dirty=0, entry=json.dumps(entry),
                        digest=_digest(report.model_dump(mode="json")), lo=lo, hi=hi,
                        comparisons=_has_dependencies(report),
                        generated=_micros(report.generated_at), json_stamp=json_stamp, html_stamp=html_stamp)
            if item["href"] == latest_href:
                newest = report
            rendered += 1
            completed.add(item["href"])

    fence()
    if partial:
        stored_entries = repository.manifest_entries()
        entry_values = dict(stored_entries)
        for href in previous.keys() - items.keys():
            entry_values.pop(href, None)
        for href, item in items.items():
            if item["entry"] is None:
                entry_values.pop(href, None)
            else:
                entry_values[href] = item["entry"]
        entries = sorted((json.loads(value) for value in entry_values.values()),
                         key=lambda entry: (entry["kind"], entry["start"]))
        manifest_changed = entry_values != stored_entries
    else:
        entries = [json.loads(item["entry"]) for item in sorted(items.values(),
                   key=lambda row: (row["kind"], row["start"])) if item["entry"] is not None]
        manifest_changed = any(previous.get(href, {}).get("entry") != item["entry"]
                               for href, item in items.items()) or bool(previous.keys() - items.keys())
    if cloud:
        meta["storage_target"] = storage_target
        meta["storage_layout"] = "period-v1"
        if latest is None:
            meta["latest_key"] = ""
            meta["latest_etag"] = ""
        if (manifest_changed or recovering or not meta.get("manifest_key")
                or not manifest_current):
            meta["manifest_key"], meta["manifest_etag"] = cloud.manifest(entries, now, fence=fence)
        if newest is not None:
            fence()
            meta["latest_key"], meta["latest_etag"] = cloud.latest(render(newest, is_latest=True), fence=fence)
    else:
        if (manifest_changed or recovering or meta.get("manifest_stamp") != _stamp(manifest_path)
                or not manifest_path.exists()):
            manifest = {"version": 1, "updated_at": now.astimezone(UTC).isoformat(), "reports": entries}
            pub.atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", mode=0o644)
        if newest is not None:
            fence()
            pub._write_changed(output / "latest.html", render(newest, is_latest=True))
        meta["manifest_stamp"] = _stamp(manifest_path)
        meta["latest_stamp"] = _stamp(output / "latest.html")
    meta["checkpoint"] = str(upper)
    meta["count"] = str(len(entries))
    if partial:
        unpublished = (int(old_meta["unpublished_count"])
                       + sum(item["entry"] is None for item in items.values())
                       - sum(item["entry"] is None for item in previous.values()))
        if unpublished < 0:
            raise ValueError("publication unpublished count became negative")
    else:
        unpublished = sum(item["entry"] is None for item in items.values())
    meta["unpublished_count"] = str(unpublished)
    meta["unpublished_basis"] = _unpublished_basis(meta)
    meta["latest_report_id"] = latest["report_id"] if latest else ""
    fence()
    if not repository.save(items, previous, meta, old_meta, expected_checkpoint=checkpoint,
                           lease_owner=owner, lease_attempt=attempt,
                           queue_events={"added": len(added), "completed": len(completed),
                                         "requeued": sum(requeued.values())}):
        raise RuntimeError("publisher lost its YDB checkpoint or lease")
    pending, oldest = repository.pending_count() if partial else (
        sum(bool(item["dirty"]) for item in items.values()),
        min((item["queued"] for item in items.values() if item["dirty"]), default=None),
    )
    return {"reports": len(entries), "rendered_reports": rendered,
            "queue_metrics": queue_metrics(meta),
            "pending_reports": pending,
            "pending_oldest_timestamp_seconds": oldest / 1_000_000 if oldest is not None else None,
            "manifest": meta["manifest_key"] if cloud else str(manifest_path),
            "latest_report_id": latest["report_id"] if latest else None}
