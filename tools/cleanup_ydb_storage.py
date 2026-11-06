"""Private export, reviewed plan and bounded cleanup of redundant YDB storage.

Only configuration snapshots and recognized legacy chart packets may be changed.
Exports/plans contain private bytes and must remain outside the repository.
Run in Docker with private YDB environment settings; no schema initialization.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tools.cleanup_chart_cache import apply_chart_cleanup, plan_chart_cleanup
from tools.cleanup_configuration_snapshots import apply_configuration_cleanup, plan_configuration_cleanup
from tools.compact_configuration_export import compact
from tools.ydb_storage_audit import _inventory
from zont_analyzer.adapters.ydb.database import YdbConfig, YdbDatabase
from zont_analyzer.adapters.ydb.telemetry import encode

EXPORT_COLUMNS = {
    "config_snapshots": ["device_id", "content_hash", "id", "payload", "captured_at"],
    "reports": ["id", "payload", "revision", "kind", "period_start", "period_end", "algorithm_version"],
    "app_meta": ["key", "value"],
    "devices": ["id", "payload"],
    "sequences": ["name", "value"],
}


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scope(config: YdbConfig) -> str:
    return hashlib.sha256(encode({"endpoint": config.endpoint, "database": config.database,
                                 "namespace": config.namespace}).encode()).hexdigest()


def _write(path: Path, value: Any) -> None:
    with open(path, "x", encoding="utf-8", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
        json.dump(value, stream, ensure_ascii=False)


def _private_path(path: Path) -> None:
    if path.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError("private artifacts must be outside the repository")


def export_storage(db: YdbDatabase, directory: Path, *, max_rows: int = 30000) -> dict[str, Any]:
    """Snapshot each selected table independently; apply fences handle later writes."""
    _private_path(directory)
    if not 1 <= max_rows <= 100000:
        raise ValueError("invalid export row budget")
    directory.mkdir(mode=0o700)
    manifest: dict[str, Any] = {"version": 1, "scope_sha256": _scope(db.config),
                              "created_at": datetime.now(UTC).isoformat(), "tables": {}}
    for table, columns in EXPORT_COLUMNS.items():
        def consume(rows: Any) -> list[dict[str, Any]]:
            result = []
            for row in rows:
                result.append(dict(row))
                if len(result) > max_rows:
                    raise ValueError("export row budget exceeded")
            return result

        rows = db.read_table(table, columns=columns, key_range=None, consume=consume)
        path = directory / (table + ".jsonl")
        with open(path, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
            for row in rows:
                stream.write(encode(row) + "\n")
        manifest["tables"][table] = {"rows": len(rows), "sha256": _sha(path), "export_bytes": path.stat().st_size}
    manifest["physical_statistics"] = _inventory(db)
    _write(directory / "manifest.json", manifest)
    return manifest


def load_export(directory: Path) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    _private_path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("version") != 1 or set(manifest["tables"]) != set(EXPORT_COLUMNS):
        raise ValueError("incomplete or unsupported export")
    tables = {}
    for table in EXPORT_COLUMNS:
        path = directory / (table + ".jsonl")
        if _sha(path) != manifest["tables"][table]["sha256"]:
            raise ValueError("export checksum mismatch")
        with path.open() as stream:
            tables[table] = [json.loads(line) for line in stream]
        if len(tables[table]) != manifest["tables"][table]["rows"]:
            raise ValueError("export row count mismatch")
    return manifest, tables


def create_plan(directory: Path) -> dict[str, Any]:
    manifest, tables = load_export(directory)
    plan = {"version": 1, "scope_sha256": manifest["scope_sha256"],
            "manifest_sha256": _sha(directory / "manifest.json"),
            "configuration": plan_configuration_cleanup(tables["config_snapshots"]),
            "charts": plan_chart_cleanup(tables["reports"], tables["app_meta"])}
    _write(directory / "plan.json", plan)
    return {"plan_sha256": _sha(directory / "plan.json"),
            "configuration": plan["configuration"]["summary"], "charts": plan["charts"]["summary"]}


def load_plan(directory: Path, expected_sha: str) -> dict[str, Any]:
    if _sha(directory / "plan.json") != expected_sha:
        raise ValueError("reviewed plan checksum mismatch")
    manifest, tables = load_export(directory)
    expected: dict[str, Any] = {"version": 1, "scope_sha256": manifest["scope_sha256"],
                                "manifest_sha256": _sha(directory / "manifest.json"),
                                "configuration": plan_configuration_cleanup(tables["config_snapshots"]),
                                "charts": plan_chart_cleanup(tables["reports"], tables["app_meta"])}
    # Rebuild from the checksummed export and compare the exact serialized plan.
    # Streaming avoids parsing another copy of hundreds of MB of source bytes.
    rebuilt_sha = hashlib.sha256()
    for chunk in json.JSONEncoder(ensure_ascii=False).iterencode(expected):
        rebuilt_sha.update(chunk.encode("utf-8"))
    if rebuilt_sha.hexdigest() != expected_sha:
        raise ValueError("plan does not match the immutable source export")
    return expected


def verify_exports(before: Path, after: Path) -> dict[str, Any]:
    """Strict offline comparison; concurrent changes are reported, never concealed."""
    bm, bt = load_export(before)
    am, at = load_export(after)
    if bm["scope_sha256"] != am["scope_sha256"]:
        raise ValueError("export database scope mismatch")
    expected, config_summary = compact(bt["config_snapshots"])
    config_summary.pop("production_modified")
    def config_key(row: dict[str, Any]) -> tuple[str, str]:
        return row["device_id"], row["content_hash"]
    expected_map = {config_key(row): row for row in expected}
    actual_map = {config_key(row): row for row in at["config_snapshots"]}
    source_ids = {(row["device_id"], row["id"]) for row in bt["config_snapshots"]}
    old_actual = {config_key(row): row for row in at["config_snapshots"]
                  if (row["device_id"], row["id"]) in source_ids}
    plan = plan_chart_cleanup(bt["reports"], bt["app_meta"])
    meta_after = {row["key"]: row["value"] for row in at["app_meta"]}
    before_reports = {row["id"]: row for row in bt["reports"]}
    after_reports = {row["id"]: row for row in at["reports"]}
    protected = {row["key"]: row["value"] for row in bt["app_meta"]
                 if not row["key"].startswith("chart-data:")}
    missing_protected = sum(key not in meta_after for key in protected)
    changed_protected = sum(key in meta_after and meta_after[key] != value for key, value in protected.items())
    missing_reports = sum(key not in after_reports for key in before_reports)
    changed_reports = sum(key in after_reports and after_reports[key] != value
                          for key, value in before_reports.items())
    sources_left = sum(key in meta_after for action in plan["report_plans"] for key in action["sources"])
    changed_targets = sum(meta_after.get(action["target_key"]) != action["after_target"]
                          for action in plan["report_plans"])
    return {
        "configuration": {**config_summary, "retained_targets_equal": all(actual_map.get(k) == v
                          for k, v in expected_map.items()), "source_timeline_equal": old_actual == expected_map,
                          "new_observations": len(actual_map) - len(old_actual)},
        "charts": {**plan["summary"], "planned_legacy_rows_remaining": sources_left,
                   "changed_or_missing_targets": changed_targets},
        "protected": {"reports_missing": missing_reports, "reports_changed": changed_reports,
                      "report_rows_before": len(before_reports), "report_rows_after": len(after_reports),
                      "metadata_missing": missing_protected, "metadata_changed": changed_protected},
        "strict_comparison_passed": (old_actual == expected_map and not
                                     (sources_left or changed_targets or missing_reports or changed_reports
                                      or missing_protected or changed_protected)),
        "physical_statistics_before": bm["physical_statistics"],
        "physical_statistics_after": am["physical_statistics"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("directory", type=Path)
    export.add_argument("--max-rows", type=int, default=30000)
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("directory", type=Path)
    apply = sub.add_parser("apply")
    apply.add_argument("directory", type=Path)
    apply.add_argument("--plan-sha256", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("before", type=Path)
    verify.add_argument("after", type=Path)
    args = parser.parse_args()
    if args.command == "plan":
        print(json.dumps(create_plan(args.directory)))
        return
    if args.command == "verify":
        result = verify_exports(args.before, args.after)
        print(json.dumps(result))
        if not result["strict_comparison_passed"]:
            raise SystemExit(2)
        return
    plan = load_plan(args.directory, args.plan_sha256) if args.command == "apply" else None
    config = YdbConfig.from_environment()
    if plan is not None and plan["scope_sha256"] != _scope(config):
        raise ValueError("plan database scope mismatch")
    db = YdbDatabase(config)
    try:
        if args.command == "export":
            manifest = export_storage(db, args.directory, max_rows=args.max_rows)
            result = {"tables": manifest["tables"], "physical_statistics": manifest["physical_statistics"]}
        else:
            assert plan is not None
            configuration = apply_configuration_cleanup(db, plan["configuration"])
            if configuration["status"] != "complete":
                print(json.dumps({"configuration": configuration, "charts": "not_started"}))
                raise SystemExit(2)
            result = {"configuration": configuration, "charts": apply_chart_cleanup(db, plan["charts"])}
        print(json.dumps(result))
        if args.command == "apply" and result["charts"]["conflicted_reports"]:
            raise SystemExit(2)
    finally:
        db.close()


if __name__ == "__main__":
    main()
