"""Bounded local #65 comparison; invoked by deploy/measure-ydb-bounds.sh.

Server RU observed locally are not a managed database billing measurement.
Private SDK interception is deliberately restricted to this disposable harness.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import statistics
import threading
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import grpc
import ydb
from google.protobuf.json_format import MessageToDict
from ydb.connection import _RpcState

from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.adapters.ydb.database import YdbConfig
from zont_analyzer.cloud import monitoring
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import TelemetryPoint


class ObserveRpc:
    def __init__(self):
        self.records = []
        self.pending = []
        self._old = _RpcState.__call__

    def __enter__(self):
        owner = self

        def call(state, request, *args, **kwargs):
            tracked = state.rpc_name in {
                "ExecuteQuery",
                "BeginTransaction",
                "CommitTransaction",
                "RollbackTransaction",
            }
            if not tracked:
                return owner._old(state, request, *args, **kwargs)
            if state.rpc_name == "ExecuteQuery":
                request.stats_mode = ydb.QueryStatsMode.FULL.value
            record = {
                "rpc": state.rpc_name,
                "server_ru": None,
                "status": None,
                "stats": None,
                "complete": False,
                # Inspect only locally; SQL and parameters are never exported.
                "expects_updates": state.rpc_name == "ExecuteQuery" and bool(re.search(
                    r"\b(?:UPSERT|INSERT|UPDATE|REPLACE)\b", request.query_content.text, re.IGNORECASE,
                )),
                "expects_deletes": state.rpc_name == "ExecuteQuery" and bool(re.search(
                    r"\bDELETE\b", request.query_content.text, re.IGNORECASE,
                )),
            }
            owner.records.append(record)
            done = threading.Event()
            owner.pending.append(done)

            def finish(raw):
                try:
                    record["grpc_status"] = str(raw.code())
                    # Read both locations, prefer trailers; never add same header twice.
                    initial = dict(raw.initial_metadata() or ())
                    trailing = dict(raw.trailing_metadata() or ())
                    value = trailing.get(
                        "x-ydb-consumed-units", initial.get("x-ydb-consumed-units")
                    )
                    record["server_ru"] = None if value is None else float(value)
                    record["complete"] = True
                finally:
                    done.set()

            try:
                response = owner._old(state, request, *args, **kwargs)
            except grpc.RpcError as exc:
                finish(exc)
                raise
            if hasattr(response, "__next__"):
                if not response.add_callback(lambda: finish(response)):
                    finish(response)
                return Stream(response, record)
            record["status"] = (
                int(response.status) if hasattr(response, "status") else None
            )
            finish(state.rendezvous)
            return response

        _RpcState.__call__ = call
        return self

    def __exit__(self, *args):
        _RpcState.__call__ = self._old
        for event in self.pending:
            event.wait(2)

    def summary(self):
        records = self.records
        full = [r for r in records if r["stats"] is not None]
        stats = [r["stats"] for r in full]
        compile_us = sum(
            int(s.get("compilation", {}).get("cpu_time_us", 0)) for s in stats
        )
        process_us = sum(int(s.get("process_cpu_time_us", 0)) for s in stats)
        phase_us = sum(
            int(p.get("cpu_time_us", 0))
            for s in stats
            for p in s.get("query_phases", [])
        )
        total_us = sum(int(s.get("total_cpu_time_us", 0)) for s in stats)
        accesses = [
            a
            for s in stats
            for p in s.get("query_phases", [])
            for a in p.get("table_access", [])
        ]
        def missing_write_stats(record, kind):
            return record.get("expects_" + kind, False) and not any(
                kind in access
                for phase in (record["stats"] or {}).get("query_phases", [])
                for access in phase.get("table_access", [])
            )

        missing_updates = sum(missing_write_stats(record, "updates") for record in records)
        missing_deletes = sum(missing_write_stats(record, "deletes") for record in records)
        return {
            "rpc_count": len(records),
            "rpc_counts": dict(Counter(r["rpc"] for r in records)),
            "server_ru": sum(r["server_ru"] for r in records)
            if records and all(r["server_ru"] is not None for r in records)
            else None,
            "ru_present_rpc_count": sum(r["server_ru"] is not None for r in records),
            "ru_missing_rpc_count": sum(r["server_ru"] is None for r in records),
            "failed_rpc_count": sum(
                r["status"] not in (None, 400000)
                or r.get("grpc_status") != "StatusCode.OK"
                for r in records
            ),
            "all_rpc_complete": all(r["complete"] for r in records),
            "queries_with_stats": len(full),
            "write_stats_complete": not (missing_updates or missing_deletes),
            "update_stats_missing_rpc_count": missing_updates,
            "delete_stats_missing_rpc_count": missing_deletes,
            "compile_cpu_us": compile_us,
            "process_cpu_us": process_us,
            "phase_cpu_us": phase_us,
            "query_cpu_us": total_us,
            "query_cpu_components_us": compile_us + process_us + phase_us,
            "queries_with_total_cpu": sum("total_cpu_time_us" in s for s in stats),
            "result_rows": sum(r.get("result_rows", 0) for r in records),
            "read_rows": sum(int(a.get("reads", {}).get("rows", 0)) for a in accesses),
            "read_bytes": sum(
                int(a.get("reads", {}).get("bytes", 0)) for a in accesses
            ),
            "update_rows": None if missing_updates else sum(
                int(a.get("updates", {}).get("rows", 0)) for a in accesses
            ),
            "update_bytes": None if missing_updates else sum(
                int(a.get("updates", {}).get("bytes", 0)) for a in accesses
            ),
            "delete_rows": None if missing_deletes else sum(
                int(a.get("deletes", {}).get("rows", 0)) for a in accesses
            ),
        }


class Stream:
    def __init__(self, raw, record):
        self.raw, self.record = raw, record

    def __iter__(self):
        return self

    def __next__(self):
        part = next(self.raw)
        self.record["status"] = int(part.status)
        if part.HasField("result_set"):
            self.record["result_rows"] = self.record.get("result_rows", 0) + len(
                part.result_set.rows
            )
        if part.HasField("exec_stats"):
            # FULL snapshots are cumulative: retain final snapshot once per RPC.
            self.record["stats"] = MessageToDict(
                part.exec_stats, preserving_proto_field_name=True
            )
        return part

    def __getattr__(self, key):
        return getattr(self.raw, key)


def baseline_patch(directory):
    """Load only old affected methods into the otherwise identical fixture runtime."""
    from zont_analyzer.adapters.ydb import application, telemetry

    selected = [
        (
            application,
            Database,
            "application.py",
            ["_sample_boundary", "earliest_sample_time", "latest_sample_time"],
        ),
        (
            telemetry,
            telemetry.TelemetryRepository,
            "telemetry.py",
            ["_write_window_batch"],
        ),
        (monitoring, None, "monitoring.py", ["_snapshot"]),
    ]
    for module, owner, filename, names in selected:
        tree = ast.parse((directory / filename).read_text())
        nodes = (
            tree.body
            if owner is None
            else next(
                n.body
                for n in tree.body
                if isinstance(n, ast.ClassDef) and n.name == owner.__name__
            )
        )
        for name in names:
            node = next(
                n for n in nodes if isinstance(n, ast.FunctionDef) and n.name == name
            )
            # Keep module globals live so metric capture also wraps the old function.
            scope = vars(module)
            exec(  # noqa: S102 - selected methods from the explicitly chosen Git revision
                compile(ast.Module(body=[node], type_ignores=[]), filename, "exec"),
                scope,
            )
            setattr(owner or module, name, scope[name])


START = datetime(2015, 1, 1, tzinfo=UTC)


def point(series, stamp, value=1.0):
    return TelemetryPoint(
        device_id="fixture",
        entity_id=f"sensor{series}",
        source_type="fixture",
        metric_key="temperature",
        timestamp_utc=stamp,
        value_num=value,
    )


def write(db, stamp, value=1.0):
    return db.telemetry.write_window(
        device_id="fixture",
        data_type="fixture",
        start=stamp,
        end=stamp + timedelta(seconds=1),
        points=[point(0, stamp, value)],
    )


def seed(db, years, series):
    end = START.replace(year=START.year + years)
    # Establish catalogue through real writes, then insert deterministic history in batches.
    db.telemetry.write_window(
        device_id="fixture",
        data_type="fixture",
        start=START,
        end=end,
        points=[point(s, t) for s in range(series) for t in (START, end)],
    )
    ids = sorted(int(s["id"]) for s in db.list_series())
    row_type = (
        ydb.StructType()
        .add_member("series_id", ydb.PrimitiveType.Int64)
        .add_member("timestamp_utc", ydb.PrimitiveType.Int64)
        .add_member("value_num", ydb.PrimitiveType.Double)
        .add_member("quality", ydb.PrimitiveType.Utf8)
    )
    rows = []
    for day in range(1, (end - START).days):
        stamp = int((START + timedelta(days=day)).timestamp())
        for sid in ids:
            rows.append(
                {
                    "series_id": sid,
                    "timestamp_utc": stamp,
                    "value_num": 1.0,
                    "quality": "valid",
                }
            )
        if len(rows) >= 1000:
            db.storage.execute(
                "DECLARE $rows AS List<Struct<series_id:Int64,timestamp_utc:Int64,value_num:Double,quality:Utf8>>; "
                "UPSERT INTO telemetry_samples SELECT * FROM AS_TABLE($rows);",
                {"$rows": ydb.TypedValue(rows, ydb.ListType(row_type))},
            )
            rows = []
    if rows:
        db.storage.execute(
            "DECLARE $rows AS List<Struct<series_id:Int64,timestamp_utc:Int64,value_num:Double,quality:Utf8>>; "
            "UPSERT INTO telemetry_samples SELECT * FROM AS_TABLE($rows);",
            {"$rows": ydb.TypedValue(rows, ydb.ListType(row_type))},
        )
    return end, len(ids) * ((end - START).days + 1)


def record(args, db, scenario, repeat, fn, **extra):
    with ObserveRpc() as observed:
        started = time.perf_counter()
        result = fn()
        elapsed = time.perf_counter() - started
    print(
        json.dumps(
            {
                "label": args.label,
                "source_sha": args.source_sha,
                "years": args.years,
                "series": args.series,
                "scenario": scenario,
                "repeat": repeat,
                "elapsed_seconds": elapsed,
                "result_sha256": hashlib.sha256(
                    json.dumps(result, sort_keys=True, default=str).encode()
                ).hexdigest(),
                **observed.summary(),
                **extra,
            },
            sort_keys=True,
        ),
        flush=True,
    )


def summarize(path):
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    indexed = {
        (r["years"], r["label"], r["scenario"], r["repeat"]): r
        for r in rows
        if "repeat" in r
    }
    groups = []
    for years, scenario in sorted(
        {(r["years"], r["scenario"]) for r in indexed.values()}
    ):
        for label in ("baseline", "candidate"):
            selected = [
                r
                for r in indexed.values()
                if (r["years"], r["scenario"], r["label"]) == (years, scenario, label)
            ]
            result = {
                "years": years,
                "scenario": scenario,
                "label": label,
                "repeats": len(selected),
            }
            for name in (
                "server_ru",
                "rpc_count",
                "query_cpu_us",
                "read_rows",
                "read_bytes",
                "update_rows",
                "elapsed_seconds",
            ):
                values = [r[name] for r in selected]
                result[name] = (
                    None
                    if any(v is None for v in values)
                    else {
                        "min": min(values),
                        "median": statistics.median(values),
                        "max": max(values),
                    }
                )
            groups.append(result)
    mismatches = []
    for (years, label, scenario, repeat), row in indexed.items():
        if label != "baseline":
            continue
        other = indexed.get((years, "candidate", scenario, repeat))
        if other is None or other["result_sha256"] != row["result_sha256"]:
            mismatches.append({"years": years, "scenario": scenario, "repeat": repeat})
    print(
        json.dumps(
            {
                "measurement_source": "local_server_x-ydb-consumed-units",
                "managed_billing_proven": False,
                "semantic_mismatches": mismatches,
                "groups": groups,
            },
            sort_keys=True,
        )
    )
    if mismatches:
        raise SystemExit("baseline/candidate semantic mismatch")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summarize", type=Path)
    parser.add_argument("--label")
    parser.add_argument("--namespace")
    parser.add_argument("--years", type=int, choices=(1, 5, 10))
    parser.add_argument("--series", type=int, default=47)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--baseline-dir", type=Path)
    parser.add_argument("--source-sha")
    args = parser.parse_args()
    if args.summarize:
        summarize(args.summarize)
        return
    if not all((args.label, args.namespace, args.years, args.source_sha)):
        parser.error("label, namespace, years, source-sha required for measurement")
    if not 1 <= args.series <= 64 or not 3 <= args.repeats <= 10:
        parser.error("series 1..64 and repeats 3..10 required")
    if args.baseline_dir:
        baseline_patch(args.baseline_dir)
    db = Database(YdbConfig("grpc://ydb:2136", "/local", args.namespace, True))
    try:
        db.initialize()
        started = time.perf_counter()
        end, rows = seed(db, args.years, args.series)
        print(
            json.dumps(
                {
                    "label": args.label,
                    "years": args.years,
                    "scenario": "seed",
                    "sample_rows": rows,
                    "elapsed_seconds": time.perf_counter() - started,
                }
            ),
            flush=True,
        )
        runtime = SimpleNamespace(db=db, config=AppConfig())
        def bounds():
            return (list(db.sample_bounds()) if not args.baseline_dir
                    else [db.earliest_sample_time(), db.latest_sample_time()])
        # Deliberately exercise bootstrap from an existing unmaterialized history.
        db.storage.execute("DELETE FROM app_meta WHERE key='telemetry-bounds:v1';")
        record(args, db, "bounds_cold", 0, bounds)
        # monitoring observer returns no data; capture exported metrics for semantic comparison.
        exported = []
        old_observe = monitoring.observe
        monitoring.observe = lambda name, value, **labels: exported.append(
            (name, float(value), labels)
        )
        try:
            for repeat in range(args.repeats):
                record(args, db, "bounds_warm", repeat, bounds)
                exported.clear()
                record(
                    args,
                    db,
                    "monitoring_warm",
                    repeat,
                    lambda: (monitoring._snapshot(runtime), exported)[1],
                )
                stamp = end + timedelta(days=repeat + 1)
                record(
                    args, db, "write_extends_bound", repeat, lambda stamp=stamp: write(db, stamp)
                )
                record(
                    args, db, "write_identical_replay", repeat, lambda stamp=stamp: write(db, stamp)
                )
                record(
                    args,
                    db,
                    "write_changes_value",
                    repeat,
                    lambda stamp=stamp: write(db, stamp, 2.0),
                )
                record(
                    args,
                    db,
                    "write_late_point",
                    repeat,
                    lambda repeat=repeat: write(db, START - timedelta(days=repeat + 1)),
                )
            record(args, db, "bounds_final", 0, bounds)
        finally:
            monitoring.observe = old_observe
    finally:
        db.close()


if __name__ == "__main__":
    main()
