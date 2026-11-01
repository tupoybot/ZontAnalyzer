"""Native local-only coverage comparison; use deploy/measure-coverage-cost.sh.

The baseline runs without the additive coverage index, since YDB can otherwise
select it even for the old query. Fixture rows are half-hour source windows;
one ten-year straddler must survive both recent-window filters.
"""
from __future__ import annotations

import argparse
import ast
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import ydb  # type: ignore[import-untyped]

from tools.measure_ydb_bounds import ObserveRpc
from zont_analyzer.adapters.ydb.database import YdbConfig, YdbDatabase
from zont_analyzer.adapters.ydb.read_indexes import ensure_read_indexes


def _query(path: Path, needle: str) -> str:
    values = [node.value for node in ast.walk(ast.parse(path.read_text()))
              if isinstance(node, ast.Constant) and isinstance(node.value, str) and needle in node.value]
    if len(values) != 1:
        raise ValueError(f"expected one query containing {needle}")
    return values[0]


def _wait(operation: Any) -> None:
    deadline = time.monotonic() + 60
    while not operation.ready and time.monotonic() < deadline:
        time.sleep(0.1)
        operation = operation.get()
    if not operation.ready:
        raise TimeoutError("disposable coverage index removal did not finish")


def _seed(db: YdbDatabase, years: int, end: int) -> int:
    row_type = (ydb.StructType().add_member("device_id", ydb.PrimitiveType.Utf8)
                .add_member("data_type", ydb.PrimitiveType.Utf8)
                .add_member("started_at", ydb.PrimitiveType.Int64)
                .add_member("ended_at", ydb.PrimitiveType.Int64)
                .add_member("state", ydb.PrimitiveType.Utf8)
                .add_member("checked_at", ydb.PrimitiveType.Int64))
    batch = []
    count = 0

    def write() -> None:
        db.driver.table_client.bulk_upsert(db.path + "/coverage", batch, row_type)

    first = end - years * 365 * 86400
    for start in range(first, end, 1800):
        batch.append({"device_id": "fixture", "data_type": "temperature", "started_at": start,
                      "ended_at": start + 1800, "state": "complete", "checked_at": (start + 1800) * 1_000_000})
        count += 1
        if len(batch) == 1000:
            write()
            batch.clear()
    batch.extend([
        {"device_id": "fixture", "data_type": "temperature", "started_at": end - 11 * 365 * 86400,
         "ended_at": end + 3600, "state": "empty", "checked_at": end * 1_000_000},
        {"device_id": "fixture", "data_type": "temperature", "started_at": end - 600,
         "ended_at": end, "state": "failed", "checked_at": end * 1_000_000},
    ])
    write()
    return count + 2


def _measure(db: YdbDatabase, case: str, variant: str, years: int, repetition: int,
             query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
    started = time.monotonic()
    with ObserveRpc() as rpc:
        result = db.execute(query, parameters)
    cost = rpc.summary()
    if not cost["all_rpc_complete"] or cost["ru_missing_rpc_count"] or cost["failed_rpc_count"]:
        raise RuntimeError("native comparison has incomplete RU or failed RPC coverage")
    rows = [dict(row) for part in result for row in part.rows]
    print(json.dumps({"case": case, "variant": variant, "years": years, "repetition": repetition,
                      "elapsed_ms": (time.monotonic() - started) * 1000, "result": rows,
                      "native": cost}, sort_keys=True), flush=True)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--years", type=int, choices=(1, 5, 10), required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        raise ValueError("repeats must be between one and ten")
    root = Path(__file__).resolve().parents[1]
    end = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())
    start = end - int(timedelta(minutes=30).total_seconds())
    bounds = {"$device": "fixture", "$type": "temperature", "$start": start, "$end": end}
    coverage = {**bounds, "$after_start": -(2**63), "$after_end": -(2**63),
                "$limit": ydb.TypedValue(1000, ydb.PrimitiveType.Uint64)}
    replay = {**bounds, "$since": (end - 900) * 1_000_000}
    db = YdbDatabase(YdbConfig("grpc://ydb:2136", "/local", f"coverage_cost_{args.years}", anonymous=True))
    try:
        db.initialize()
        session = db.driver.table_client.session().create()
        try:
            _wait(session.alter_table(db.path + "/coverage", drop_indexes=["by_end"]))
        finally:
            session.delete()
        count = _seed(db, args.years, end)
        print(json.dumps({"fixture": "half_hour_windows_and_long_straddler", "years": args.years,
                          "coverage_rows": count, "repeats": args.repeats}), flush=True)
        expected = {}
        for variant in ("baseline", "candidate"):
            if variant == "candidate":
                ensure_read_indexes(db)
            source = args.baseline_dir if variant == "baseline" else root / "src/zont_analyzer"
            queries = {
                "coverage": (_query(source / "adapters/ydb/telemetry.py",
                                     "SELECT started_at,ended_at,state FROM coverage "), coverage),
                "recent_replay": (_query(source / "application/collection.py",
                                          "SELECT started_at,ended_at FROM coverage "), replay),
            }
            queries["coverage_resume"] = (queries["coverage"][0], {
                **coverage, "$after_start": end - 11 * 365 * 86400, "$after_end": end + 3600,
            })
            queries["recent_replay_retry"] = queries["recent_replay"]
            for repetition in range(args.repeats):
                for case, (query, parameters) in queries.items():
                    rows = _measure(db, case, variant, args.years, repetition, query, parameters)
                    if variant == "baseline" and repetition == 0:
                        expected[case] = rows
                    if rows != expected[case] or len(rows) != (3 if case == "coverage" else 2):
                        raise AssertionError("coverage/replay result changed or lost the long straddler")
                write_query = (
                    "DECLARE $start AS Int64; DECLARE $end AS Int64; DECLARE $probe AS Utf8; "
                    "UPSERT INTO coverage (device_id,data_type,started_at,ended_at,state,checked_at) "
                    "VALUES ('fixture',$probe,$start,$end,'complete',0);"
                )
                write_parameters = {"$start": start + repetition, "$end": end + repetition,
                                    "$probe": "write_before" if variant == "baseline" else "write_after_"}
                _measure(db, "coverage_insert", variant, args.years, repetition, write_query, write_parameters)
                _measure(db, "coverage_write_retry", variant, args.years, repetition, write_query, write_parameters)
    finally:
        db.close()


if __name__ == "__main__":
    main()
