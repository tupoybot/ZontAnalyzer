"""Pair baseline and current YDB report lookups using full query statistics.

Baseline methods are loaded verbatim from the supplied commit's application.py
and reports.py. The normal mode is read-only and suitable for one bounded
managed-YDB comparison. ``--local-history-years`` seeds disposable local YDB
namespaces and runs the same comparison at synthetic history sizes.
"""

from __future__ import annotations

import argparse
import ast
import json
import textwrap
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import ydb  # type: ignore[import-untyped]

from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.adapters.ydb.database import Transaction, YdbConfig, YdbDatabase
from zont_analyzer.adapters.ydb.reports import ReportRepository, _seconds
from zont_analyzer.adapters.ydb.telemetry import utc_seconds
from zont_analyzer.domain.models import QualityResult, Report

BASELINE_COMMIT = "3149665386e5a31dc0fe64d7edd2803137022069"


@dataclass
class Cost:
    queries: int = 0
    result_rows: int = 0
    result_bytes: int = 0
    read_rows: int = 0
    read_bytes: int = 0
    cpu_us: int = 0
    queries_with_stats: int = 0
    request_units: int = 0
    request_units_available: bool = True

    def add(self, results: list[Any], stats: Any) -> None:
        self.queries += 1
        for part in results:
            if part is None:
                continue
            for row in part.rows:
                self.result_rows += 1
                self.result_bytes += len(json.dumps(dict(row), default=str).encode())
        if stats is None:
            self.request_units_available = False
            return
        self.queries_with_stats += 1
        self.cpu_us += int(stats.process_cpu_time_us)
        for phase in stats.query_phases:
            for access in phase.table_access:
                self.read_rows += int(access.reads.rows)
                self.read_bytes += int(access.reads.bytes)
        ru = next((getattr(stats, field) for field in (
            "request_units", "total_request_units", "request_units_consumed",
        ) if getattr(stats, field, None) is not None), None)
        if ru is None:
            self.request_units_available = False
        else:
            self.request_units += int(ru)


class Observe:
    """Request FULL stats from both direct and transactional SDK query paths."""

    def __init__(self, storage: YdbDatabase) -> None:
        self.storage = storage
        self.cost = Cost()
        self._pool_execute = storage.pool.execute_with_retries
        self._tx_execute = Transaction.execute

    def __enter__(self) -> Cost:
        costs, pool = self.cost, self.storage.pool

        def pool_execute(
            query: str,
            parameters: dict[str, Any] | None = None,
            retry_settings: Any = None,
            *args: Any,
            **kwargs: Any,
        ) -> list[Any]:
            def run(session: Any) -> list[Any]:
                with session.execute(
                    query, parameters, *args, stats_mode=ydb.QueryStatsMode.FULL, **kwargs,
                ) as stream:
                    results = list(stream)
                costs.add(results, session.last_query_stats)
                return cast(list[Any], results)

            return cast(list[Any], pool.retry_operation_sync(run, retry_settings))

        def tx_execute(
            tx: Transaction, query: str, parameters: dict[str, Any] | None = None,
        ) -> list[Any]:
            with tx.raw.execute(
                tx.prefix + query, parameters=parameters, stats_mode=ydb.QueryStatsMode.FULL,
            ) as stream:
                parts = []
                for part in stream:
                    if part is None:
                        continue
                    if part.truncated:
                        raise ValueError("YDB returned a truncated result")
                    parts.append(part)
            costs.add(parts, tx.raw.last_query_stats)
            merged: dict[int, Any] = {}
            for part in parts:
                index = int(part.index or 0)
                if index in merged:
                    merged[index].rows.extend(part.rows)
                else:
                    merged[index] = part
            return [merged[index] for index in sorted(merged)]

        pool.execute_with_retries = pool_execute
        Transaction.execute = tx_execute  # type: ignore[method-assign]
        return self.cost

    def __exit__(self, *_args: object) -> None:
        self.storage.pool.execute_with_retries = self._pool_execute
        Transaction.execute = self._tx_execute  # type: ignore[method-assign]


def _source_methods(path: Path, class_name: str, names: set[str]) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    target = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    methods = [
        ast.get_source_segment(source, node)
        for node in target.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    found = {ast.parse(method).body[0].name for method in methods if method is not None}
    if found != names:
        raise ValueError(f"baseline source did not contain expected methods for {class_name}")
    return [textwrap.indent(textwrap.dedent(method or ""), "    ") for method in methods]


def _baseline_types(app_source: Path, report_source: Path, storage: YdbDatabase) -> tuple[Any, Any]:
    namespace: dict[str, Any] = {
        "Any": Any, "Iterator": Iterator, "Report": Report, "json": json,
        "datetime": datetime, "UTC": UTC, "utc_seconds": utc_seconds, "ydb": ydb,
        "_seconds": _seconds,
    }
    app_methods = _source_methods(
        app_source, "Database", {"report_for_period", "completed_reports", "_all_reports"},
    )
    reports_methods = _source_methods(report_source, "ReportRepository", {"prior_reports"})
    reports_methods += _source_methods(report_source, "ReportRepository", {"completed_reports"})
    exec("class BaselineDatabase:\n" + "\n".join(app_methods), namespace)
    exec("class BaselineReports:\n" + "\n".join(reports_methods), namespace)
    namespace["BaselineReports"]._list = ReportRepository._list
    namespace["BaselineReports"]._load = staticmethod(ReportRepository._load)
    legacy_db = namespace["BaselineDatabase"]()
    legacy_db.storage = storage
    legacy_reports = namespace["BaselineReports"]()
    legacy_reports.db = storage
    legacy_db.reports = legacy_reports
    return legacy_db, legacy_reports


def _measure(storage: YdbDatabase, case: str, version: str, call: Callable[[], Any]) -> Any:
    with Observe(storage) as cost:
        started = time.perf_counter()
        result = call()
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
    print(json.dumps({
        "baseline_commit": BASELINE_COMMIT,
        "case": case,
        "version": version,
        "queries": cost.queries,
        "result_rows": cost.result_rows,
        "result_bytes": cost.result_bytes,
        "read_rows": cost.read_rows,
        "read_bytes": cost.read_bytes,
        "cpu_us": cost.cpu_us,
        "queries_with_stats": cost.queries_with_stats,
        "request_units": cost.request_units if cost.request_units_available else None,
        "duration_ms": duration_ms,
        "result_count": len(result) if isinstance(result, list) else int(result is not None),
    }, sort_keys=True), flush=True)
    return result


def _latest_old(legacy_reports: Any, now: datetime) -> tuple[datetime | None, bool]:
    # OwnerContextStore.gas historically called ReportRepository.completed_reports,
    # which loads at most 1000 payloads. Database.completed_reports is a different
    # API path and would overstate the old implementation's read cost.
    complete = legacy_reports.completed_reports(now)
    latest = max((report.period_start for report in complete if report.kind == "daily"), default=None)
    return latest, len(complete) >= 1000


def _paired(storage: YdbDatabase, db: Database, legacy_db: Any, legacy_reports: Any,
            now: datetime, history: str) -> None:
    current_prior = _measure(storage, f"{history}_prior_7", "current",
                             lambda: db.prior_reports(now, limit=7))
    baseline_prior = _measure(storage, f"{history}_prior_7", "baseline",
                              lambda: legacy_reports.prior_reports(now, limit=7))
    if [item.id for item in current_prior] != [item.id for item in baseline_prior]:
        raise AssertionError("prior-seven report selections changed")
    if current_prior:
        target = current_prior[0]
        baseline_period = _measure(
            storage, f"{history}_single_period", "baseline",
            lambda: legacy_db.report_for_period(target.period_start, target.period_end),
        )
        current_period = _measure(
            storage, f"{history}_single_period", "current",
            lambda: db.report_for_period(target.period_start, target.period_end),
        )
        if (baseline_period is None or current_period is None
                or baseline_period.id != current_period.id):
            raise AssertionError("single-period report selection changed")
    baseline_latest_info = _measure(
        storage, f"{history}_latest_daily", "baseline", lambda: _latest_old(legacy_reports, now),
    )
    baseline_latest, baseline_truncated = baseline_latest_info
    current_latest = _measure(
        storage, f"{history}_latest_daily", "current",
        lambda: db.latest_completed_daily_report_start(now),
    )
    if not baseline_truncated and baseline_latest != current_latest:
        raise AssertionError("latest completed daily report changed")
    if current_prior and current_latest != current_prior[0].period_start:
        raise AssertionError("current latest daily report differs from the newest prior report")
    print(json.dumps({
        "history": history,
        "prior_and_period_equivalent": True,
        "baseline_latest_matches_current": baseline_latest == current_latest,
        "baseline_latest_truncated_at_1000": baseline_truncated,
    }, sort_keys=True), flush=True)


def _fixture_row(report_id: str, start: datetime) -> dict[str, Any]:
    end = start + timedelta(days=1)
    report = Report(
        id=report_id,
        kind="daily",
        period_start=start,
        period_end=end,
        generated_at=end + timedelta(minutes=1),
        quality=QualityResult(
            score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
            implausible_jumps=0, sample_count=1,
        ),
        summary="Synthetic report history " + ("x" * 2048),
    )
    return {
        "kind": report.kind,
        "period_start": utc_seconds(report.period_start),
        "period_end": utc_seconds(report.period_end),
        "algorithm_version": report.algorithm_version,
        "id": report.id,
        "payload": json.dumps(
            {"report": report.model_dump(mode="json"), "rendered_text": ""},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ),
        "revision": 1,
    }


def _seed_local(storage: YdbDatabase, years: int) -> int:
    storage.initialize()
    days = years * 365
    last_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    rows = [
        _fixture_row(f"report-cost-{years}y-{index:05d}", last_start - timedelta(days=days - index - 1))
        for index in range(days)
    ]
    row_type = (
        ydb.StructType()
        .add_member("kind", ydb.PrimitiveType.Utf8)
        .add_member("period_start", ydb.PrimitiveType.Int64)
        .add_member("period_end", ydb.PrimitiveType.Int64)
        .add_member("algorithm_version", ydb.PrimitiveType.Utf8)
        .add_member("id", ydb.PrimitiveType.Utf8)
        .add_member("payload", ydb.PrimitiveType.Utf8)
        .add_member("revision", ydb.PrimitiveType.Int64)
    )
    for offset in range(0, len(rows), 250):
        storage.execute(
            "DECLARE $rows AS List<Struct<kind:Utf8,period_start:Int64,period_end:Int64,"
            "algorithm_version:Utf8,id:Utf8,payload:Utf8,revision:Int64>>; "
            "UPSERT INTO reports SELECT * FROM AS_TABLE($rows);",
            {"$rows": ydb.TypedValue(rows[offset:offset + 250], ydb.ListType(row_type))},
        )
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-application", type=Path, required=True)
    parser.add_argument("--baseline-reports", type=Path, required=True)
    parser.add_argument("--local-history-years", type=int, nargs="+")
    args = parser.parse_args()
    commit_files = {"application": args.baseline_application, "reports": args.baseline_reports}
    if args.local_history_years:
        base_config = YdbConfig.from_environment()
        if (not base_config.endpoint.startswith("grpc://") or base_config.database != "/local"
                or not base_config.anonymous):
            parser.error("local history seeding requires anonymous grpc:// YDB at /local")
        for years in args.local_history_years:
            if years not in {1, 5, 10}:
                parser.error("local history sizes must be 1, 5, or 10 years")
            config = YdbConfig(
                base_config.endpoint, base_config.database, f"report_cost_{years}y", anonymous=True,
            )
            storage = YdbDatabase(config)
            try:
                count = _seed_local(storage, years)
                print(json.dumps({"history": f"{years}y", "seeded_reports": count}, sort_keys=True), flush=True)
                db = Database(storage)
                legacy_db, legacy_reports = _baseline_types(
                    commit_files["application"], commit_files["reports"], storage,
                )
                _paired(storage, db, legacy_db, legacy_reports, datetime.now(UTC), f"{years}y")
            finally:
                storage.close()
    else:
        config = YdbConfig.from_environment()
        storage = YdbDatabase(config)
        try:
            db = Database(storage)
            legacy_db, legacy_reports = _baseline_types(
                commit_files["application"], commit_files["reports"], storage,
            )
            revision_before = db.source_revision()
            _paired(storage, db, legacy_db, legacy_reports, datetime.now(UTC), "managed")
            revision_after = db.source_revision()
            stable = revision_before == revision_after
            print(json.dumps({"history": "managed", "source_revision_stable": stable}, sort_keys=True), flush=True)
            if not stable:
                raise RuntimeError("managed report inputs changed during paired measurement")
        finally:
            storage.close()


if __name__ == "__main__":
    main()
