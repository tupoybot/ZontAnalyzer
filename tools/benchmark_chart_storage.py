"""Paired chart-cache revisions on disposable local YDB, without production data.

Canonical report saves and fixture validation are excluded from cache RPC costs.
The deterministic packet builder replaces telemetry reads equally in both variants.
"""
from __future__ import annotations

import argparse
import json
import time
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tools.measure_ydb_bounds import ObserveRpc
from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.adapters.ydb.database import YdbConfig
from zont_analyzer.domain import QualityResult, Report
from zont_analyzer.reports import chart_data


def _baseline(path: Path) -> Any:
    module = types.ModuleType("baseline_chart_data")
    exec(compile(path.read_text(), str(path), "exec"), module.__dict__)
    return module


def _report(revision: int) -> Report:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return Report(
        id="fixture-report", kind="daily", period_start=start, period_end=start + timedelta(days=1),
        generated_at=start + timedelta(days=1), timezone="UTC", summary=f"revision-{revision:03d}", context={},
        quality=QualityResult(score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
                              implausible_jumps=0, sample_count=1),
    )


def _packet(report: Report) -> dict[str, Any]:
    return {"timezone": "UTC", "summary": report.summary,
            "series": {"fixture": {"points": [
                {"timestamp": f"2026-01-01T{index // 60:02d}:{index % 60:02d}:00+00:00", "value": index % 25}
                for index in range(180)
            ]}}}


def _inventory(db: Database) -> dict[str, int]:
    rows = db.storage.execute(
        "SELECT key,value FROM app_meta WHERE key >= 'chart-data:' AND key < 'chart-data;';",
    )[0].rows
    return {"rows": len(rows), "v2_rows": sum(row.key.startswith("chart-data:v2:") for row in rows),
            "v3_rows": sum(row.key.startswith("chart-data:v3:") for row in rows),
            "key_utf8_bytes": sum(len(row.key.encode()) for row in rows),
            "value_utf8_bytes": sum(len(row.value.encode()) for row in rows)}


def _costs(records: list[Any]) -> dict[str, Any]:
    observer = ObserveRpc()
    observer.records = records
    writes = [record for record in records if record["expects_updates"] or record["expects_deletes"]]
    return {"native": observer.summary(), "write_query_rpc_count": len(writes),
            "write_query_server_ru": (sum(record["server_ru"] for record in writes)
                                      if all(record["server_ru"] is not None for record in writes) else None)}


def _run(old: Any, variant: str, revisions: int, legacy: bool = False) -> dict[str, Any]:
    db = Database(YdbConfig("grpc://ydb:2136", "/local", f"chart_{variant}_{revisions}_{int(legacy)}", True))
    module = old if variant == "baseline" else chart_data
    original = module.build_chart_data
    builds = 0

    def build(_db: Any, report: Report) -> dict[str, Any]:
        nonlocal builds
        builds += 1
        if legacy:
            raise AssertionError("legacy packet must be retained without rebuilding telemetry")
        return _packet(report)

    module.build_chart_data = build
    try:
        db.initialize()
        if legacy:
            report = _report(0)
            db.save_report(report, report.summary)
            key, digest = old._cache_key(report)
            db.set_app_meta(key, json.dumps({"schema_version": old.CHART_DATA_SCHEMA_VERSION,
                                            "report_digest": digest, "data": _packet(report)}))
        before = _inventory(db)
        records: list[Any] = []
        seconds = 0.0
        for revision in range(revisions):
            report = _report(revision)
            # Canonical saves are setup, not part of cache generation costs.
            db.save_report(report, report.summary)
            with ObserveRpc() as observer:
                started = time.perf_counter()
                first = module.cached_chart_data(db, report)
                second = module.cached_chart_data(db, report)
                seconds += time.perf_counter() - started
            records.extend(observer.records)
            assert first == second == _packet(report)
            assert db.report(report.id) == report
        after = _inventory(db)
        if legacy:
            assert builds == 0 and before["v2_rows"] == after["v2_rows"] == 1
            assert after["v3_rows"] == 1
        else:
            assert builds == revisions
            assert after["rows"] == (revisions if variant == "baseline" else 1)
        return {
            "scenario": "legacy_fallback" if legacy else "repeated_report_revisions",
            "variant": variant, "revisions": revisions, "cache_reads_per_revision": 2,
            "fixture_packet_utf8_bytes": len(json.dumps(_packet(_report(0)), separators=(",", ":")).encode()),
            "cache_before": before, "cache_after": after, "packet_build_count": builds,
            "seconds": round(seconds, 6), **_costs(records),
            "packet_equality_verified": True, "canonical_report_unchanged_by_cache": True,
            "limitations": [
                "Cache costs exclude canonical report saves and verification reads.",
                "Deterministic synthetic packets replace telemetry reads in both variants.",
                "Native local consumed units are not managed-service billed request units.",
                "Logical UTF-8 bytes exclude table storage overhead, indexes, replication and compression.",
                "Legacy rows remain present; this benchmark performs no historical cleanup.",
            ],
        }
    finally:
        module.build_chart_data = original
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-chart", type=Path, required=True)
    args = parser.parse_args()
    old = _baseline(args.baseline_chart)
    for revisions in (48, 96):
        for variant in ("baseline", "candidate"):
            print(json.dumps(_run(old, variant, revisions), sort_keys=True), flush=True)
    print(json.dumps(_run(old, "candidate", 1, legacy=True), sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
