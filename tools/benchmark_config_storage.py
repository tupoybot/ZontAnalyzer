"""Compare configuration storage on disposable local YDB, never managed YDB.

Both variants receive identical synthetic periodic runtime responses and A-B-A
settings. Native RPC costs and logical payload sizes are separate measurements.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tools.measure_ydb_bounds import ObserveRpc
from tools.ydb_storage_audit import _inventory
from zont_analyzer.adapters.ydb.database import Transaction, YdbConfig, YdbDatabase
from zont_analyzer.adapters.ydb.telemetry import TelemetryRepository, encode
from zont_analyzer.adapters.zont_readonly.configuration import configuration_snapshot


def _baseline(path: Path) -> Any:
    import zont_analyzer.adapters.ydb.telemetry as telemetry

    tree = ast.parse(path.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == "TelemetryRepository")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "save_devices")
    scope = dict(vars(telemetry))
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), scope)
    return scope["save_devices"]


def _devices(count: int, changes: bool) -> list[dict[str, Any]]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    result = []
    for index in range(count):
        setting = 23 if changes and count // 3 <= index < 2 * count // 3 else 21
        result.append({
            "id": "fixture", "name": "synthetic heating controller", "model": "fixture",
            "discovered_at": (start + timedelta(hours=6 * index)).isoformat(),
            "raw": {"id": "fixture", "name": "synthetic heating controller", "online": index % 2 == 0,
                    "last_contact": index, "weather": {"temperature": index % 15},
                    "io": {"z3k-state": {"sensor": {"temperature": index % 30, "target": index % 25}}},
                    "z3k_config": {"heating_circuits": [{"id": 1, "target": setting}],
                                   "fixture_configuration": "x" * 4096}},
        })
    return result


def _projections(raw_per_event: float, candidate_per_event: float, physical_per_event: float | None) -> list[Any]:
    return [{
        "years": years, "discovery_days_per_year": days, "discoveries_per_day": cadence,
        "configuration_transitions_per_year": 2, "initial_seed_excluded": True,
        "baseline_additional_snapshot_rows": years * days * cadence,
        "candidate_additional_snapshot_rows": years * 2,
        "baseline_additional_logical_payload_bytes": round(years * days * cadence * raw_per_event),
        "candidate_additional_logical_payload_bytes": round(years * 2 * candidate_per_event),
        "candidate_additional_physical_bytes_linear_estimate": (
            None if physical_per_event is None else round(years * 2 * physical_per_event)),
    } for cadence in (4, 24) for days in (180, 365) for years in (1, 5, 10)]


def _run(old_save: Any, variant: str, count: int, changes: bool) -> dict[str, Any]:
    # Endpoint and database are fixed to the dedicated benchmark Docker network.
    db = YdbDatabase(YdbConfig("grpc://ydb:2136", "/local",
                              f"config_{variant}_{count}_{int(changes)}", True))
    try:
        db.initialize()
        repository = TelemetryRepository(db)
        devices = _devices(count, changes)
        writes: Counter[str] = Counter()
        original = Transaction.execute

        def execute(tx: Transaction, query: str, parameters: Any = None) -> Any:
            for table in re.findall(r"\b(?:UPSERT|INSERT|UPDATE|REPLACE)\s+(?:INTO\s+)?(\w+)", query):
                writes[table] += 1
            return original(tx, query, parameters)

        Transaction.execute = execute  # type: ignore[method-assign]
        try:
            with ObserveRpc() as rpc:
                started = time.perf_counter()
                for device in devices:
                    if variant == "baseline":
                        old_save(repository, [device])
                    else:
                        repository.save_devices([device])
                seconds = time.perf_counter() - started
        finally:
            Transaction.execute = original  # type: ignore[method-assign]
        # Fixture tables contain at most 96 rows. These reads are outside costs.
        stored = repository.list_devices()
        assert len(stored) == 1 and stored[0] == devices[-1]
        snapshots = db.execute("SELECT id,payload,captured_at FROM config_snapshots ORDER BY id;")[0].rows
        payload_bytes = sum(len(row.payload.encode("utf-8")) for row in snapshots)
        observed_configs = [configuration_snapshot(json.loads(row.payload)) for row in snapshots]
        expected_configs = []
        for device in devices:
            config = configuration_snapshot(device)
            if not expected_configs or expected_configs[-1] != config:
                expected_configs.append(config)
        retained_changes = []
        for config in observed_configs:
            if not retained_changes or retained_changes[-1] != config:
                retained_changes.append(config)
        assert retained_changes == expected_configs
        if variant == "candidate":
            assert len(snapshots) == (3 if changes else 1)
        inventory = [item for item in _inventory(db)
                     if item["table"] in ("devices", "config_snapshots", "app_meta", "revisions", "sequences")]
        physical = next(item for item in inventory if item["table"] == "config_snapshots")
        if physical["physical_rows"] != len(snapshots):
            physical["error"] = "asynchronous_statistics_do_not_match_fixture"
        physical_per_row = (
            physical["physical_bytes"] / len(snapshots)
            if physical["physical_rows"] == len(snapshots) and physical["physical_bytes"] else None
        )
        return {
            "variant": variant, "discoveries": count, "setting_A_B_A": changes,
            "seconds": round(seconds, 6), "native": rpc.summary(),
            "write_query_rpc_count": sum(record["expects_updates"] or record["expects_deletes"]
                                         for record in rpc.records),
            "write_query_server_ru": (
                sum(record["server_ru"] for record in rpc.records
                    if record["expects_updates"] or record["expects_deletes"])
                if all(record["server_ru"] is not None for record in rpc.records
                       if record["expects_updates"] or record["expects_deletes"]) else None),
            "transaction_rpc_counts": {key: value for key, value in rpc.summary()["rpc_counts"].items()
                                       if key != "ExecuteQuery"},
            "issued_upsert_statements_by_table": dict(writes),
            "snapshot_rows": len(snapshots), "snapshot_logical_payload_bytes": payload_bytes,
            "latest_catalogue_matches_input": True, "configuration_transitions_preserved": True,
            "physical_inventory": inventory,
            "projections": _projections(
                sum(len(encode(device).encode()) for device in devices) / count,
                sum(len(encode(config).encode()) for config in expected_configs) / len(expected_configs),
                physical_per_row if variant == "candidate" else None,
            ),
            "limitations": [
                "Local YDB native RPC statistics are not managed-service billed request units.",
                "Physical statistics are asynchronous; unavailable or stale estimates do not become zero.",
                "Growth explores 4 and 24 discoveries daily (scheduler default: 24), "
                "with 2 configuration transitions per season.",
                "Logical payload projections exclude keys, indexes, replicas and compression.",
                "Physical linear extrapolation is exploratory and excludes all other application tables.",
                "Telemetry history is unchanged and may dominate total storage over five to ten seasons.",
                "Issued statements count client attempts, not committed rows or write RPC billing.",
            ],
        }
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-telemetry", type=Path, required=True)
    args = parser.parse_args()
    old_save = _baseline(args.baseline_telemetry)
    for count in (48, 96):
        for changes in (False, True):
            for variant in ("baseline", "candidate"):
                print(json.dumps(_run(old_save, variant, count, changes), sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
