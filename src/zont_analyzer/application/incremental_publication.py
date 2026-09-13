"""Bounded publication queue; the index is disposable, canonical data is not.

Only recovery walks the archive. Input changes are committed with source writes;
queue/checkpoint commits happen after atomic artifact publication. A crash can
repeat work, but cannot acknowledge work whose artifacts were not completed.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from zont_analyzer.adapters.sqlite.publication_journal import changes_since
from zont_analyzer.domain import Report

if TYPE_CHECKING:
    from zont_analyzer.runtime import Runtime

# Increment when rendering/derived-calculation semantics change.
VERSION = "publication-v1"
RENDER, GAS, COST = 1, 2, 4
AUDIT_SIZE = 16


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _stamp(path: Path) -> str:
    try:
        stat = path.stat()
        return f"{stat.st_mtime_ns}:{stat.st_size}"
    except FileNotFoundError:
        return ""


def _meta(cache: sqlite3.Connection, key: str, default: str = "") -> str:
    row = cache.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else default


def _set(cache: sqlite3.Connection, key: str, value: str) -> None:
    cache.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))


def _open(path: Path) -> sqlite3.Connection:
    def connect() -> sqlite3.Connection:
        cache = sqlite3.connect(path)
        cache.row_factory = sqlite3.Row
        try:
            cache.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS items(
                    href TEXT PRIMARY KEY, report_id TEXT NOT NULL, kind TEXT NOT NULL,
                    start REAL NOT NULL, end REAL NOT NULL, generated REAL NOT NULL,
                    digest TEXT NOT NULL, lo REAL NOT NULL, hi REAL NOT NULL, comparisons INTEGER NOT NULL,
                    dirty INTEGER NOT NULL, queued REAL NOT NULL,
                    entry TEXT, json_stamp TEXT NOT NULL DEFAULT '', html_stamp TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS item_report ON items(report_id);
                CREATE INDEX IF NOT EXISTS item_queue ON items(queued) WHERE dirty!=0;
                CREATE INDEX IF NOT EXISTS item_latest ON items(kind,start DESC);
            """)
            columns = {row[1] for row in cache.execute("PRAGMA table_info(items)")}
            required = {"href", "report_id", "kind", "start", "end", "generated", "digest", "lo", "hi",
                        "dirty", "queued", "entry", "json_stamp", "html_stamp", "comparisons"}
            if not required <= columns:
                raise sqlite3.DatabaseError("obsolete publication cache schema")
        except sqlite3.DatabaseError:
            cache.close()
            raise
        return cache
    try:
        return connect()
    except sqlite3.DatabaseError:
        # Derived index only; no report/telemetry database or export is deleted.
        path.unlink(missing_ok=True)
        return connect()


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


def _enqueue(
    cache: sqlite3.Connection, flags: int, now: datetime, where: str = "1", args: tuple[Any, ...] = (),
) -> None:
    cache.execute(
        f"UPDATE items SET queued=CASE WHEN dirty=0 THEN ? ELSE queued END, dirty=dirty|? WHERE {where}",
        (now.timestamp(), flags | RENDER, *args),
    )


def _remember(
    cache: sqlite3.Connection, output: Path, report: Report, now: datetime, *, retained: bool = False,
) -> bool:
    from zont_analyzer.application.publication import KINDS, archive_paths

    if report.kind not in KINDS or report.period_end > now or report.generated_at < report.period_end:
        return False
    html_path, json_path = archive_paths(output, report)
    href = html_path.relative_to(output).as_posix()
    previous = cache.execute("SELECT * FROM items WHERE href=?", (href,)).fetchone()
    digest = _digest(report.model_dump(mode="json"))
    if previous and (previous["generated"] > report.generated_at.timestamp() or previous["digest"] == digest):
        return False
    lo, hi = _windows(report)
    entry = previous["entry"] if previous else None
    if retained and html_path.is_file():
        entry = json.dumps(_entry(output, report))
    cache.execute("""
        INSERT INTO items(href,report_id,kind,start,end,generated,digest,lo,hi,comparisons,
                          dirty,queued,entry,json_stamp,html_stamp)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(href) DO UPDATE SET
        report_id=excluded.report_id,kind=excluded.kind,start=excluded.start,end=excluded.end,
        generated=excluded.generated,digest=excluded.digest,lo=excluded.lo,hi=excluded.hi,
        comparisons=excluded.comparisons,
        queued=CASE WHEN items.dirty=0 THEN excluded.queued ELSE items.queued END,
        dirty=items.dirty|excluded.dirty,entry=excluded.entry
        """, (href, report.id, report.kind, report.period_start.timestamp(), report.period_end.timestamp(),
              report.generated_at.timestamp(), digest, lo, hi, bool(report.context.get("period_comparisons")),
              RENDER | GAS, now.timestamp(), entry,
              _stamp(json_path), _stamp(html_path)))
    return True


def _dependents(cache: sqlite3.Connection, report: Report, now: datetime) -> None:
    # Comparison money reads stored gas of other periods. A bounded batch may
    # refresh those periods after their consumers; queue a cost-only correction.
    _enqueue(cache, COST, now, "comparisons=1 AND report_id!=? AND lo < ? AND hi > ?",
             (report.id, report.period_end.timestamp(), report.period_start.timestamp()))


def _scan_exports(cache: sqlite3.Connection, output: Path, now: datetime) -> None:
    from zont_analyzer.application.publication import KINDS, archive_paths

    for kind in KINDS:
        for path in (output / kind).glob("*.json"):
            href = path.with_suffix(".html").relative_to(output).as_posix()
            old = cache.execute("SELECT json_stamp FROM items WHERE href=?", (href,)).fetchone()
            if old and old[0] == _stamp(path):
                continue
            try:
                report = Report.model_validate_json(path.read_text(encoding="utf-8"))
                html_path, expected = archive_paths(output, report)
                if expected == path and html_path.is_file():
                    _remember(cache, output, report, now, retained=True)
            except (OSError, ValueError):
                continue


def _source(runtime: Runtime, output: Path, row: sqlite3.Row) -> Report | None:
    report = runtime.db.report(row["report_id"])
    try:
        exported = Report.model_validate_json((output / row["href"]).with_suffix(".json").read_text())
        if report is None or exported.generated_at > report.generated_at:
            report = exported
    except (OSError, ValueError):
        pass
    return report


def _telemetry(cache: sqlite3.Connection, runtime: Runtime, hours: list[str], now: datetime) -> None:
    if not hours:
        return
    # The gas model uses all meter intervals; late samples within calibration
    # can change estimates outside the sample's own report period.
    with runtime.db.session() as session:
        span = session.execute(text("SELECT MIN(reading_day),MAX(reading_day) FROM gas_readings")).one()
    calibration: tuple[float, float] | None = None
    if span[0] and span[1] and span[0] != span[1]:
        zone = ZoneInfo(runtime.config.home.effective_timezone)
        calibration = (
            datetime.fromisoformat(span[0]).replace(tzinfo=zone).timestamp() - 900,
            (datetime.fromisoformat(span[1]).replace(tzinfo=zone) + timedelta(days=1)).timestamp() + 900,
        )
    for hour in hours:
        start = datetime.strptime(hour, "%Y-%m-%dT%H").replace(tzinfo=UTC).timestamp()
        end = start + 3600
        if calibration and start < calibration[1] and end > calibration[0]:
            _enqueue(cache, GAS, now)
            return
        _enqueue(cache, GAS, now, "lo < ? AND hi > ?", (end, start))


def publish_incremental(
    runtime: Runtime, output: Path, now: datetime, *, batch_size: int = 8, rebuild: bool = False,
) -> dict[str, Any]:
    if not 1 <= batch_size <= 100:
        raise ValueError("publication batch_size must be between 1 and 100")
    cache = _open(output / ".publication-cache.sqlite3")
    try:
        with cache:
            return _run(cache, runtime, output, now, batch_size, rebuild)
    finally:
        cache.close()


def _run(
    cache: sqlite3.Connection, runtime: Runtime, output: Path, now: datetime, batch_size: int, rebuild: bool,
) -> dict[str, Any]:
    from zont_analyzer.application import publication as pub
    from zont_analyzer.application.ai_maintenance import review_state
    from zont_analyzer.application.gas import GasService
    from zont_analyzer.application.gas_tariffs import GasTariffStore
    from zont_analyzer.application.owner_context import OwnerContextStore
    from zont_analyzer.application.timezone import apply_device_timezone

    apply_device_timezone(runtime.db, runtime.config)
    # Capture before reading sources. Writes during publication stay pending.
    checkpoint = int(_meta(cache, "checkpoint", "0"))
    upper, changes = changes_since(runtime.db, checkpoint)
    identity = str(runtime.db.path.resolve()) + ":" + str(runtime.db.get_app_meta("instance_id"))
    recovering = rebuild or upper < checkpoint or _meta(cache, "identity") != identity
    if recovering:
        cache.execute("DELETE FROM items")
        _scan_exports(cache, output, now)
        # Metadata IDs only; never materialize the archive's canonical JSON at once.
        with runtime.db.session() as session:
            ids = session.execute(text("SELECT id FROM reports WHERE period_end<=:end ORDER BY generated_at"),
                                  {"end": int(now.timestamp())}).scalars().all()
        for report_id in ids:
            report = runtime.db.report(report_id)
            if report is not None:
                _remember(cache, output, report, now)
        _set(cache, "identity", identity)
    else:
        if _meta(cache, "directories") != _digest([_stamp(output / k) for k in pub.KINDS]):
            _scan_exports(cache, output, now)
        for change in changes:
            scope, identifier = change["scope"], change["identifier"]
            if scope == "report":
                report = runtime.db.report(identifier)
                if report is not None and _remember(cache, output, report, now):
                    _dependents(cache, report, now)
            elif scope == "render":
                _enqueue(cache, RENDER, now, "report_id=?", (identifier,))
            elif scope == "global":
                _enqueue(cache, {"gas": GAS, "cost": COST}.get(identifier, RENDER), now)
            elif scope == "tariff":
                start = datetime.fromisoformat(identifier).replace(tzinfo=UTC).timestamp()
                # Conservative until the next known tariff; display-only changes
                # to a future tariff do not recalculate historical money or gas.
                with runtime.db.session() as session:
                    next_start = session.execute(text(
                        "SELECT MIN(effective_from) FROM gas_tariffs WHERE effective_from>:start"
                    ), {"start": identifier}).scalar_one()
                end = (datetime.fromisoformat(next_start).replace(tzinfo=UTC).timestamp()
                       if next_start else float("inf"))
                _enqueue(cache, COST, now, "lo+900 < ? AND hi-900 > ?", (end, start))
        _telemetry(cache, runtime, [c["identifier"] for c in changes if c["scope"] == "telemetry"], now)

    owner_store = OwnerContextStore(runtime.db)
    profiles = [owner_store.profile(str(d["id"]), now) for d in runtime.db.list_devices()]
    # as_of is display metadata, not an input change. Effective fields catch
    # activation of a future-dated profile even when no new DB write occurs.
    profile_digest = _digest([{k: v for k, v in p.items() if k != "as_of"} for p in profiles])
    if _meta(cache, "profiles") != profile_digest:
        _enqueue(cache, GAS, now)
    _set(cache, "profiles", profile_digest)
    ai_review = review_state(runtime)
    config_digest = _digest((VERSION, runtime.config.home.model_dump(), runtime.config.home.effective_timezone,
                             runtime.config.preferences.model_dump(), runtime.config.analysis.model_dump(),
                             runtime.config.feedback.public_api_base_url))
    if _meta(cache, "config") != config_digest:
        _enqueue(cache, GAS, now)
    _set(cache, "config", config_digest)
    if _meta(cache, "ai_review") != _digest(ai_review):
        _enqueue(cache, RENDER, now)
    _set(cache, "ai_review", _digest(ai_review))

    # Bounded stat audit catches removed/corrupted artifacts without scanning all
    # paths every poll. Cache loss or --rebuild gives immediate full reconciliation.
    cursor = int(_meta(cache, "audit", "0"))
    audit = cache.execute("SELECT rowid,* FROM items WHERE rowid>? ORDER BY rowid LIMIT ?",
                          (cursor, AUDIT_SIZE)).fetchall()
    for row in audit:
        path = output / row["href"]
        if row["json_stamp"] != _stamp(path.with_suffix(".json")) or row["html_stamp"] != _stamp(path):
            _enqueue(cache, GAS, now, "href=?", (row["href"],))
    _set(cache, "audit", str(audit[-1]["rowid"]) if len(audit) == AUDIT_SIZE else "0")

    latest_row = cache.execute("SELECT * FROM items WHERE kind='daily' ORDER BY start DESC LIMIT 1").fetchone()
    latest_href = latest_row["href"] if latest_row else ""
    if latest_row and _meta(cache, "latest_stamp") != _stamp(output / "latest.html"):
        _enqueue(cache, RENDER, now, "href=?", (latest_href,))
    queue = cache.execute("""SELECT * FROM items WHERE dirty!=0
        ORDER BY CASE WHEN href=? THEN 0 ELSE 1 END,queued,rowid LIMIT ?""", (latest_href, batch_size)).fetchall()
    service = GasService(runtime.db, runtime.config) if any(r["dirty"] & (GAS | COST) for r in queue) else None
    tariffs = GasTariffStore(runtime.db).history() if queue else []

    def render(report: Report, *, latest: bool = False) -> str:
        owner_data = {"profiles": profiles, "tariffs": tariffs, "ai_review": ai_review,
                      "gas": (owner_store.gas(report.id)
                              if report.kind == "daily" and runtime.db.report(report.id) else None)}
        return pub.render_html(report, runtime.db.recommendation_views_for_report(report.id),
                               chart_data=pub.cached_chart_data(runtime.db, report),
                               feedback_api_base_url=runtime.config.feedback.public_api_base_url,
                               current_comfort_band_c=runtime.config.preferences.comfort_band_c,
                               latest_report_href="latest.html" if latest else "../latest.html", owner_data=owner_data)

    rendered = 0
    newest: Report | None = None
    for row in queue:
        report = _source(runtime, output, row)
        if report is None:
            cache.execute("DELETE FROM items WHERE href=?", (row["href"],))
            continue
        if service is not None and row["dirty"] & (GAS | COST):
            refreshed = service.refresh(report) if row["dirty"] & GAS else service.refresh_cost(report)
            if service.persist_refresh(report, refreshed):
                if row["dirty"] & GAS and report.context.get("gas") != refreshed.context.get("gas"):
                    _dependents(cache, refreshed, now)
                report = refreshed
            else:
                report = runtime.db.report(report.id) or report
        html_path, json_path = pub.archive_paths(output, report)
        pub._write_changed(html_path, render(report))
        pub._write_changed(json_path, report.model_dump_json(indent=2) + "\n")
        lo, hi = _windows(report)
        cache.execute("""UPDATE items SET dirty=0,entry=?,digest=?,lo=?,hi=?,generated=?,json_stamp=?,html_stamp=?
            WHERE href=?""", (json.dumps(_entry(output, report)), _digest(report.model_dump(mode="json")), lo, hi,
                              report.generated_at.timestamp(), _stamp(json_path), _stamp(html_path), row["href"]))
        if row["href"] == latest_href:
            newest = report
        rendered += 1

    manifest_path = output / "reports.json"
    if queue or recovering or _meta(cache, "manifest_stamp") != _stamp(manifest_path) or not manifest_path.exists():
        entries = [json.loads(r[0]) for r in cache.execute(
            "SELECT entry FROM items WHERE entry IS NOT NULL ORDER BY kind,start")]
        manifest = {"version": 1, "updated_at": now.astimezone(UTC).isoformat(), "reports": entries}
        pub.atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", mode=0o644)
    if newest is not None:
        pub._write_changed(output / "latest.html", render(newest, latest=True))
    _set(cache, "manifest_stamp", _stamp(manifest_path))
    _set(cache, "latest_stamp", _stamp(output / "latest.html"))
    _set(cache, "directories", _digest([_stamp(output / k) for k in pub.KINDS]))
    _set(cache, "checkpoint", str(upper))
    if queue or recovering:
        _set(cache, "count", str(cache.execute("SELECT COUNT(*) FROM items WHERE entry IS NOT NULL").fetchone()[0]))
    return {"reports": int(_meta(cache, "count", "0")),
            "rendered_reports": rendered,
            "pending_reports": cache.execute("SELECT COUNT(*) FROM items WHERE dirty!=0").fetchone()[0],
            "manifest": str(manifest_path), "latest_report_id": latest_row["report_id"] if latest_row else None}
