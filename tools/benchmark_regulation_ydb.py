"""Synthetic raw-source versus compact-fact reads on disposable local YDB.

Run in the test image with --anonymous-local and a disposable YDB container.
The baseline uses TelemetryRepository.read_samples, including pagination, then
calculates the full numerical oracle. The candidate reads RegulationRepository
daily payloads and aggregates them. Known synthetic catalogue metadata is held
outside BOTH measurements: this isolates data loading/calculation, not a complete
AnalysisService run, ingestion workflow, publication or billable managed-YDB RU.
Only local anonymous grpc endpoints at /local are accepted. No AI is called.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import ydb  # type: ignore[import-untyped]

from tests.regulation_acceptance_cases import START, TRIANGLE_ORACLE, daily_facts, late_peak, triangle_inputs
from tools.benchmark_report_cost import Observe
from zont_analyzer.adapters.ydb.database import YdbConfig, YdbDatabase
from zont_analyzer.adapters.ydb.regulation import RegulationRepository
from zont_analyzer.adapters.ydb.telemetry import TelemetryRepository, encode, utc_seconds
from zont_analyzer.analytics.evidence import NumericSample
from zont_analyzer.analytics.regulation import RegulationInputs, aggregate_regulation_facts, build_regulation_facts

PAGE_SIZE = 5000


def _measure(db: YdbDatabase, name: str, call: Callable[[], Any]) -> tuple[Any, dict[str, Any]]:
    with Observe(db) as cost:
        cpu, wall = time.process_time(), time.perf_counter()
        result = call()
        cpu_seconds, wall_seconds = time.process_time() - cpu, time.perf_counter() - wall
    output = asdict(cost)
    query_ru = output.pop("request_units")
    output["query_stats_request_units"] = query_ru if cost.request_units_available else None
    output.update(case=name, client_cpu_seconds=cpu_seconds, wall_seconds=wall_seconds,
                  billable_request_units=None, ru_interpretation="local diagnostic counters; not managed billing")
    if cost.native.get("failed_rpc_count") or not cost.native.get("all_rpc_complete", False):
        raise RuntimeError(f"incomplete or failed local YDB RPC measurement: {name}")
    if cost.queries_with_stats != cost.queries:
        raise RuntimeError(f"missing full query statistics: {name}")
    return result, output


def _sample_type() -> Any:
    return (ydb.StructType().add_member("series_id", ydb.PrimitiveType.Int64)
            .add_member("timestamp_utc", ydb.PrimitiveType.Int64)
            .add_member("value_num", ydb.OptionalType(ydb.PrimitiveType.Double))
            .add_member("value_text", ydb.OptionalType(ydb.PrimitiveType.Utf8))
            .add_member("quality", ydb.PrimitiveType.Utf8)
            .add_member("ingested_at", ydb.PrimitiveType.Int64))


def _seed(db: YdbDatabase, data: RegulationInputs, facts: list[dict[str, Any]],
          ids: dict[str, int]) -> dict[str, int]:
    """Setup is excluded from paired read costs, using the real table schemas."""
    rows = []
    count = 0
    row_type = _sample_type()
    for role, series in data.signals.items():
        for sample in series.samples:
            rows.append({"series_id": ids[role], "timestamp_utc": utc_seconds(sample.timestamp),
                         "value_num": sample.value, "value_text": None, "quality": "valid", "ingested_at": 0})
            count += 1
            if len(rows) == PAGE_SIZE:
                db.driver.table_client.bulk_upsert(db.path + "/telemetry_samples", rows, row_type)
                rows.clear()
    if rows:
        db.driver.table_client.bulk_upsert(db.path + "/telemetry_samples", rows, row_type)
    repository = RegulationRepository(db)
    for fact in facts:
        assert repository.save(fact, source_fingerprint="synthetic-original-v1", publish=False)
    return {"source_rows": count, "daily_fact_rows": len(facts)}


def _load(db: YdbDatabase, template: RegulationInputs, ids: dict[str, int],
          start: datetime, end: datetime) -> RegulationInputs:
    repository = TelemetryRepository(db)
    signals = {}
    for role, series in template.signals.items():
        samples, after = [], None
        while True:
            # Include the measured right endpoint; interpolation never extends
            # the last measurement. The normal repository interval is [a,b).
            page = repository.read_samples(ids[role], start, end + timedelta(seconds=1),
                                           after=after, limit=PAGE_SIZE)
            samples.extend(NumericSample(datetime.fromtimestamp(row["timestamp_utc"], UTC), row["value_num"])
                           for row in page)
            if len(page) < PAGE_SIZE:
                break
            after = int(page[-1]["timestamp_utc"])
        if series.metadata.value_kind == "setpoint" and (not samples or samples[0].timestamp > start):
            result = db.execute(
                "DECLARE $id AS Int64; DECLARE $start AS Int64; "
                "SELECT timestamp_utc,value_num FROM telemetry_samples "
                "WHERE series_id=$id AND timestamp_utc <= $start ORDER BY timestamp_utc DESC LIMIT 1;",
                {"$id": ids[role], "$start": utc_seconds(start)},
            )[0].rows
            if result:
                samples.insert(0, NumericSample(datetime.fromtimestamp(result[0].timestamp_utc, UTC),
                                                 result[0].value_num))
        signals[role] = replace(series, samples=tuple(samples))
    return replace(template, signals=signals)


def _raw(db: YdbDatabase, template: RegulationInputs, ids: dict[str, int],
         start: datetime, end: datetime) -> dict[str, Any]:
    return build_regulation_facts(start=start, end=end, inputs=_load(db, template, ids, start, end))


def _compact(db: YdbDatabase, scope: str, days: int) -> dict[str, Any]:
    end = START + timedelta(days=days)
    facts = RegulationRepository(db).read(scope, START, end)
    return aggregate_regulation_facts(facts, start=START, end=end, expected_days=days)


def _agree(actual: dict[str, Any], expected: dict[str, Any]) -> float:
    assert actual["cycle_counts"] == expected["cycle_counts"]
    for name in ("fact_seconds", "observed_seconds", "eligible_seconds"):
        assert actual["coverage"][name] == expected["coverage"][name], name
    errors = []
    for name, metric in expected["metrics"].items():
        other = actual["metrics"][name]
        assert math.isclose(other["denominator"], metric["denominator"], abs_tol=1e-8), name
        if metric["value"] is None:
            assert other["value"] is None, name
        else:
            errors.append(abs(other["value"] - metric["value"]))
            assert math.isclose(other["value"], metric["value"], rel_tol=1e-9, abs_tol=1e-8), name
    return max(errors, default=0)


def _paired(db: YdbDatabase, template: RegulationInputs, ids: dict[str, int], days: int,
            repeats: int) -> dict[str, Any]:
    end, scope = START + timedelta(days=days), template.device_id + "/" + template.circuit_id
    measurements, original = [], None
    for repetition in range(repeats):
        full, raw_cost = _measure(db, f"{days}d_raw_source_{repetition}",
                                 lambda: _raw(db, template, ids, START, end))
        compact, compact_cost = _measure(db, f"{days}d_compact_cache_{repetition}",
                                         lambda: _compact(db, scope, days))
        difference = _agree(compact, full)
        assert compact["cycle_counts"]["full"] == days * 8 - 1
        assert compact["cycle_counts"]["partial"] == 1
        assert compact["cycle_examples_total"] == days * 8
        assert compact["coverage"]["eligible_seconds"] == days * 86400
        for name, expected in TRIANGLE_ORACLE.items():
            assert math.isclose(compact["metrics"][name]["value"], expected, abs_tol=1e-8), name
        if original is not None:
            assert compact == original, "repeated database reads changed numerical facts"
        original = compact
        measurements.append({"repetition": repetition, "raw_source": raw_cost, "compact_cache": compact_cost,
                             "max_metric_difference": difference})
    return {"days": days, "cycle_counts": original["cycle_counts"],
            "all_metrics_denominators_coverage_and_independent_oracle_agree": True,
            "repeated_reads_identical": True, "measurements": measurements}


def _late(db: YdbDatabase, data: RegulationInputs, ids: dict[str, int], days: int) -> dict[str, Any]:
    day = 35
    late = late_peak(data, day=day)
    timestamp = START + timedelta(days=day, minutes=110)
    sample = next(point for point in late.signals["room"].samples if point.timestamp == timestamp)
    _, write_cost = _measure(db, "late_single_source_row_update", lambda: db.execute(
        "DECLARE $id AS Int64; DECLARE $at AS Int64; DECLARE $value AS Double; "
        "UPSERT INTO telemetry_samples (series_id,timestamp_utc,value_num,value_text,quality,ingested_at) "
        "VALUES ($id,$at,$value,NULL,'valid',1);",
        {"$id": ids["room"], "$at": utc_seconds(timestamp), "$value": sample.value}))
    left, right = START + timedelta(days=day), START + timedelta(days=day + 1)
    scope = data.device_id + "/" + data.circuit_id
    repository = RegulationRepository(db)

    def replace_day() -> dict[str, Any]:
        previous = repository.read(scope, left - timedelta(days=1), left)
        before = repository.read(scope, left, right)
        assert len(previous) == len(before) == 1
        replacement = build_regulation_facts(start=left, end=right, inputs=_load(db, data, ids, left, right),
                                             previous_boundary=previous[0]["boundary"])
        # Storage JSON normalizes numeric endpoint tuples into lists. Compare
        # the persisted representation rather than Python container types.
        assert encode(replacement["boundary"]) == encode(before[0]["boundary"]), (
            "dependent days require continued recomputation")
        assert repository.save(replacement, source_fingerprint="synthetic-late-v2", publish=False)
        return replacement

    replacement, replacement_cost = _measure(db, "late_load_recompute_replace_one_day", replace_day)
    compact, compact_cost = _measure(db, "late_compact_cache", lambda: _compact(db, scope, days))
    full, full_cost = _measure(db, "late_full_source_oracle",
                              lambda: _raw(db, data, ids, START, START + timedelta(days=days)))
    difference = _agree(compact, full)
    assert math.isclose(compact["metrics"]["room_signed_error_c"]["value"],
                        .1 + 30 / (days * 86400), abs_tol=1e-8)
    assert math.isclose(compact["metrics"]["cycle_mean_overshoot_c"]["value"],
                        .8 + .1 / (days * 8 - 1), abs_tol=1e-8)
    saved, repeat_save_cost = _measure(db, "late_identical_save_retry", lambda: repository.save(
        replacement, source_fingerprint="synthetic-late-v2", publish=False))
    assert saved is False, "identical retry rewrote the cached day"
    repeated, repeat_cost = _measure(db, "late_repeat_compact_cache", lambda: _compact(db, scope, days))
    assert repeated == compact
    assert compact["cycle_counts"]["full"] == days * 8 - 1
    return {"changed_day_index": day, "recomputed_days": 1, "boundary_converged": True,
            "max_metric_difference_from_full_source": difference, "hand_calculated_late_delta_agrees": True,
            "identical_retry_writes_nothing": True, "repeated_reads_identical": True,
            "measurements": {"source_row_update": write_cost, "day_replacement": replacement_cost,
                             "compact_cache": compact_cost, "full_source_oracle": full_cost,
                             "identical_save_retry": repeat_save_cost, "repeat_compact_cache": repeat_cost}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anonymous-local", action="store_true", required=True)
    parser.add_argument("--endpoint", default="grpc://ydb:2136")
    parser.add_argument("--database", default="/local")
    parser.add_argument("--days", type=int, nargs="+", choices=(1, 7, 60), default=[1, 7, 60])
    parser.add_argument("--repeats", type=int, choices=range(2, 6), default=2)
    args = parser.parse_args()
    endpoint = urlsplit(args.endpoint)
    if (endpoint.scheme != "grpc" or endpoint.hostname not in {"ydb", "localhost", "127.0.0.1", "::1"}
            or endpoint.username or endpoint.password or endpoint.path or endpoint.query or endpoint.fragment
            or args.database != "/local"):
        parser.error("only anonymous local grpc YDB endpoints at /local are supported")
    days = max(args.days)
    data = triangle_inputs(days)
    facts = daily_facts(data, days)
    ids = {role: index + 1 for index, role in enumerate(sorted(data.signals))}
    db = YdbDatabase(YdbConfig(args.endpoint, args.database, "regulation_cost_" + uuid4().hex, anonymous=True))
    try:
        db.initialize()
        seeded = _seed(db, data, facts, ids)
        result = {
            "schema_version": 1, "data_kind": "synthetic", "storage": "disposable anonymous local YDB",
            "namespace": "generated isolated namespace; remove with disposable database lifecycle",
            "seed_setup_excluded": seeded, "external_ai_calls": 0, "billable_request_units": None,
            "measurement_scope": "real telemetry sample pagination versus real compact-day range reads and calculation",
            "excluded_scope": ["catalogue/discovery", "ingestion lineage/revisions", "publication", "AI", "billing"],
            "scenarios": [_paired(db, data, ids, period, args.repeats) for period in args.days],
        }
        if days > 35:
            result["late_input"] = _late(db, data, ids, days)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    finally:
        db.close()


if __name__ == "__main__":
    main()
