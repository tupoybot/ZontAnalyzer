"""Paired gas-exposure cache benchmark against disposable local YDB only.

Run inside the repository test image after exporting the baseline source:

    git show 038550d:src/zont_analyzer/application/gas.py > /tmp/gas-038550d.py
    python tools/benchmark_gas_exposure.py --baseline-gas /tmp/gas-038550d.py

Uses 47 synthetic series at one-minute resolution for two days. Extra sparse
day markers model 1/5/10 years of marker history outside the measured window.
All query and timing values are local SDK measurements; managed RU is unknown.
The script writes only to its dedicated YDB namespace and stdout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import ydb  # type: ignore[import-untyped]

from tools.benchmark_ydb_cost import ObserveQueries
from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.adapters.ydb.database import YdbConfig
from zont_analyzer.application.gas import GasService
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import TelemetryPoint

BASELINE_COMMIT = "038550d"
START = datetime(2026, 9, 1, tzinfo=UTC)
END = START + timedelta(days=2)


def _baseline(path: Path) -> type:
    module = types.ModuleType("zont_analyzer.application._baseline_gas_exposure")
    module.__package__ = "zont_analyzer.application"
    exec(compile(path.read_text(), str(path), "exec"), module.__dict__)
    return module.GasService


def _clear_cache(db: Database) -> None:
    db.storage.execute(
        "DELETE FROM app_meta WHERE key >= 'gas-exposure:' AND key < 'gas-exposure;' "
        "OR key >= 'telemetry-period-revision:' AND key < 'telemetry-period-revision;';"
    )


def _seed_samples(db: Database) -> int:
    db.save_devices([{"id": "fixture-device", "name": "synthetic boiler"}])
    boiler = "z3k_boiler_adapter"
    points_per_minute: list[list[TelemetryPoint]] = []
    cursor = START - timedelta(minutes=15)
    while cursor <= END + timedelta(minutes=15):
        row = [
            TelemetryPoint(device_id="fixture-device", entity_id="boiler", source_type=boiler,
                           metric_key="s", timestamp_utc=cursor, value_text="['fl', 'ch']"),
            TelemetryPoint(device_id="fixture-device", entity_id="boiler", source_type=boiler,
                           metric_key="rml", timestamp_utc=cursor, value_num=35.0),
            TelemetryPoint(device_id="fixture-device", entity_id="outdoor", source_type="synthetic",
                           metric_key="temperature", timestamp_utc=cursor, value_num=-3.0),
        ]
        row.extend(
            TelemetryPoint(device_id="fixture-device", entity_id=f"filler-{index:02d}",
                           source_type="synthetic", metric_key="temperature",
                           timestamp_utc=cursor, value_num=float(index))
            for index in range(44)
        )
        points_per_minute.append(row)
        cursor += timedelta(minutes=1)

    roles = {"outdoor": "outdoor_temperature"}
    count = 0
    for offset in range(0, len(points_per_minute), 20):
        points = [point for minute in points_per_minute[offset:offset + 20] for point in minute]
        db.telemetry.write_window(
            device_id="fixture-device", data_type="synthetic-minute-samples",
            start=points[0].timestamp_utc, end=points[-1].timestamp_utc + timedelta(seconds=1),
            points=points, roles=roles,
        )
        count += len(points)
    return count


def _seed_sparse_markers(db: Database, years: int) -> int:
    count = years * 365
    marker_type = (ydb.StructType().add_member("key", ydb.PrimitiveType.Utf8)
                   .add_member("value", ydb.PrimitiveType.Utf8))
    marker_start = START.date() - timedelta(days=count + 30)
    db.storage.execute(
        "DECLARE $before AS Utf8; DELETE FROM app_meta WHERE key >= 'telemetry-day:' "
        "AND key < 'telemetry-day;' AND key < $before AND value LIKE 'fixture:sparse:%';",
        {"$before": f"telemetry-day:{START.date().isoformat()}"},
    )
    for offset in range(0, count, 500):
        rows = [{"key": f"telemetry-day:{(marker_start + timedelta(days=offset + i)).isoformat()}",
                 "value": f"fixture:sparse:{offset + i}"}
                for i in range(min(500, count - offset))]
        db.storage.execute(
            "DECLARE $rows AS List<Struct<key:Utf8,value:Utf8>>; "
            "UPSERT INTO app_meta SELECT * FROM AS_TABLE($rows);",
            {"$rows": ydb.TypedValue(rows, ydb.ListType(marker_type))},
        )
    return count


def _slice(service_type: type, db: Database, config: AppConfig) -> dict[str, Any]:
    service = service_type(db, config)
    return service.window(START, END)


def _digest(result: dict[str, Any]) -> str:
    payload = json.dumps(result, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _measure(service_type: type, db: Database, config: AppConfig, *, years: int,
             version: str, phase: str) -> tuple[str, dict[str, Any]]:
    with ObserveQueries(db.storage) as costs:
        begun = time.perf_counter()
        result = _slice(service_type, db, config)
        seconds = time.perf_counter() - begun
    digest = _digest(result)
    print(json.dumps({
        "years_of_sparse_markers": years,
        "version": version,
        "phase": phase,
        "exposure_sha256": digest,
        "seconds": round(seconds, 6),
        "queries": costs.queries,
        "result_rows": costs.result_rows,
        "result_json_bytes": costs.result_json_bytes,
        "sdk_read_rows": costs.sdk_read_rows,
        "sdk_read_bytes": costs.sdk_read_bytes,
        "sdk_cpu_us": costs.sdk_cpu_us,
        "queries_with_stats": costs.queries_with_stats,
        "managed_ru": None,
    }, sort_keys=True), flush=True)
    return digest, result


def _correct_historic_sample(db: Database) -> None:
    at = START + timedelta(days=1)
    point = TelemetryPoint(device_id="fixture-device", entity_id="boiler",
                           source_type="z3k_boiler_adapter", metric_key="s",
                           timestamp_utc=at, value_text="['ch']")
    db.telemetry.write_window(
        device_id="fixture-device", data_type="synthetic-historic-correction",
        start=at, end=at + timedelta(seconds=1), points=[point],
    )


def _restore_historic_sample(db: Database) -> None:
    at = START + timedelta(days=1)
    point = TelemetryPoint(device_id="fixture-device", entity_id="boiler",
                           source_type="z3k_boiler_adapter", metric_key="s",
                           timestamp_utc=at, value_text="['fl', 'ch']")
    db.telemetry.write_window(
        device_id="fixture-device", data_type="synthetic-historic-restore",
        start=at, end=at + timedelta(seconds=1), points=[point],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-gas", type=Path, required=True)
    parser.add_argument("--years", type=int, nargs="+", default=[1, 5, 10])
    parser.add_argument("--endpoint", default="grpc://ydb:2136")
    parser.add_argument("--database", default="/local")
    parser.add_argument("--namespace", default="gas_exposure_benchmark")
    args = parser.parse_args()
    if any(year not in {1, 5, 10} for year in args.years):
        parser.error("years must be selected from 1, 5, 10")
    if args.endpoint != "grpc://ydb:2136" or args.database != "/local":
        parser.error("benchmark requires disposable local Docker YDB at grpc://ydb:2136 /local")
    if not args.namespace.startswith("gas_exposure_benchmark"):
        parser.error("namespace must start with gas_exposure_benchmark")

    old_service = _baseline(args.baseline_gas)
    db = Database(YdbConfig(args.endpoint, args.database, args.namespace, True))
    try:
        db.initialize()
        db.storage.execute("DELETE FROM telemetry_samples; DELETE FROM app_meta; DELETE FROM revisions;")
        config = AppConfig()
        config.home.timezone = "UTC"
        config.analysis.modulation_capability_profile = "flame_zero_is_minimum"
        sample_count = _seed_samples(db)
        source_hash = hashlib.sha256(Path("src/zont_analyzer/application/gas.py").read_bytes()).hexdigest()
        print(json.dumps({"baseline_commit": BASELINE_COMMIT, "fixture_series": 47,
                          "fixture_samples": sample_count, "window_days": 2,
                          "candidate_gas_sha256": source_hash,
                          "storage": "disposable local YDB emulator"}, sort_keys=True), flush=True)

        for years in args.years:
            _seed_sparse_markers(db, years)
            _clear_cache(db)
            base_cold, baseline_value = _measure(old_service, db, config, years=years,
                                                  version="baseline", phase="cold")
            base_warm, _ = _measure(old_service, db, config, years=years,
                                    version="baseline", phase="warm-new-service")
            _correct_historic_sample(db)
            base_corrected, corrected_value = _measure(old_service, db, config, years=years,
                                                        version="baseline", phase="corrected-historic-new-service")
            if base_cold != base_warm or base_cold == base_corrected:
                raise AssertionError("baseline cold/warm reuse or historic correction behavior failed")
            _restore_historic_sample(db)

            _clear_cache(db)
            cand_cold, candidate_value = _measure(GasService, db, config, years=years,
                                                   version="candidate", phase="cold")
            cand_warm, _ = _measure(GasService, db, config, years=years,
                                    version="candidate", phase="warm-new-service")
            _correct_historic_sample(db)
            cand_corrected, candidate_corrected_value = _measure(
                GasService, db, config, years=years, version="candidate",
                phase="corrected-historic-new-service")
            if cand_cold != cand_warm or cand_cold == cand_corrected:
                raise AssertionError("candidate cold/warm reuse or historic correction behavior failed")
            if baseline_value != candidate_value or corrected_value != candidate_corrected_value:
                raise AssertionError(f"exposure differs between baseline and candidate for {years} years")
            print(json.dumps({"years_of_sparse_markers": years, "baseline_candidate_equal": True,
                              "corrected_result_changed": True,
                              "baseline_sha256": base_cold,
                              "candidate_sha256": cand_cold,
                              "corrected_sha256": cand_corrected}, sort_keys=True), flush=True)
            _restore_historic_sample(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
