"""Plan private chart-cache cleanup and apply one fenced transaction per report.

Plans contain exact private source bytes. Keep them outside the repository. Only
recognized legacy v2 keys belonging to a saved report are eligible for deletion;
a valid current packet must survive in v3 in the same SerializableRW transaction.
This module never reads telemetry, builds charts, or calls AI.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from zont_analyzer.adapters.ydb.database import Transaction, YdbConfig, YdbDatabase
from zont_analyzer.domain import Report
from zont_analyzer.reports.chart_data import _cache_key, _decode_chart_cache, _legacy_cache_key

_LEGACY = re.compile(r"chart-data:v2:([a-f0-9]{64}):([a-f0-9]{64})\Z")
_VERSION = 1
MAX_SOURCE_KEYS = 128
MAX_ACTION_BYTES = 8 * 1024 * 1024


def _unique(rows: Iterable[Mapping[str, Any]], key: str, value: str) -> dict[str, str]:
    result = {}
    for row in rows:
        identity, encoded = row[key], row[value]
        if not isinstance(identity, str) or not isinstance(encoded, str) or identity in result:
            raise ValueError("invalid or duplicate export identity")
        result[identity] = encoded
    return result


def plan_chart_cleanup(
    report_rows: Iterable[Mapping[str, Any]], meta_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Create a private, byte-fenced plan without modifying either export."""
    reports = _unique(report_rows, "id", "payload")
    metadata = _unique(meta_rows, "key", "value")
    legacy: dict[str, dict[str, str]] = {}
    for key, value in metadata.items():
        match = _LEGACY.fullmatch(key)
        if match:
            legacy.setdefault(match[1], {})[key] = value
    actions: list[dict[str, Any]] = []
    missing = 0
    for report_id, payload in reports.items():
        report = Report.model_validate(json.loads(payload)["report"])
        if report.id != report_id:
            raise ValueError("export report identity disagrees with payload")
        target, digest = _cache_key(report)
        sources = legacy.get(target.rsplit(":", 1)[1], {})
        if not sources:
            continue
        expected_target = metadata.get(target)
        after_target = expected_target
        if _decode_chart_cache(after_target, digest) is None:
            after_target = sources.get(_legacy_cache_key(report))
        if after_target is None or _decode_chart_cache(after_target, digest) is None:
            missing += 1
            continue
        actions.append({
            "report_id": report_id, "expected_report_payload": payload,
            "target_key": target, "expected_target": expected_target, "after_target": after_target,
            "sources": dict(sorted(sources.items())),
        })
    summary = {"report_rows": len(reports), "report_plans": len(actions),
               "legacy_rows_to_delete": sum(len(action["sources"]) for action in actions),
               "reports_without_current_packet": missing,
               "target_upserts": sum(action["after_target"] != action["expected_target"] for action in actions)}
    for action in actions:
        _validate_action(action)
    matched = {key for action in actions for key in action["sources"]}
    report_hashes = {_cache_key(Report.model_validate(json.loads(payload)["report"]))[0].rsplit(":", 1)[1]
                     for payload in reports.values()}
    orphan_rows = sum(len(rows) for report_hash, rows in legacy.items() if report_hash not in report_hashes)
    unmatched = sum(len(rows) for rows in legacy.values()) - len(matched)
    source_bytes = sum(len(value.encode("utf-8")) for action in actions for value in action["sources"].values())
    existing_target_bytes = sum(len((action["expected_target"] or "").encode("utf-8")) for action in actions)
    retained_target_bytes = sum(len(action["after_target"].encode("utf-8")) for action in actions)
    summary.update(source_payload_bytes=source_bytes, existing_target_payload_bytes=existing_target_bytes,
                   retained_target_payload_bytes=retained_target_bytes,
                   net_payload_bytes_removed=source_bytes + existing_target_bytes - retained_target_bytes,
                   unmatched_legacy_rows=unmatched, orphan_legacy_rows=orphan_rows,
                   no_current_packet_legacy_rows=unmatched - orphan_rows)
    return {"version": _VERSION, "report_plans": actions, "summary": summary}


def _validate_action(action: Mapping[str, Any]) -> None:
    report = Report.model_validate(json.loads(action["expected_report_payload"])["report"])
    target, digest = _cache_key(report)
    if report.id != action["report_id"] or target != action["target_key"]:
        raise ValueError("plan report identity mismatch")
    after = action["after_target"]
    expected = action["expected_target"]
    sources = action["sources"]
    if not isinstance(after, str) or (expected is not None and not isinstance(expected, str)):
        raise ValueError("invalid planned target bytes")
    if not isinstance(sources, dict) or not sources:
        raise ValueError("plan must include legacy source rows")
    if len(sources) > MAX_SOURCE_KEYS:
        raise ValueError("report cleanup exceeds legacy source-key limit")
    report_hash = target.rsplit(":", 1)[1]
    for key, value in sources.items():
        match = _LEGACY.fullmatch(key)
        if match is None or match[1] != report_hash or not isinstance(value, str):
            raise ValueError("plan includes an ineligible source key")
    # Include both target versions and the canonical report read, in addition
    # to all source values, to conservatively bound each transaction's payload.
    payload_bytes = sum(len(value.encode("utf-8")) for value in sources.values())
    payload_bytes += sum(len(value.encode("utf-8")) for value in (
        after, expected or "", action["expected_report_payload"]))
    if payload_bytes > MAX_ACTION_BYTES:
        raise ValueError("report cleanup exceeds transaction payload-byte limit")
    if _decode_chart_cache(after, digest) is None:
        raise ValueError("planned target is not a current chart packet")
    if after != expected and after != sources.get(_legacy_cache_key(report)):
        raise ValueError("planned target has no byte-identical current source")


def _apply_report(db: YdbDatabase, action: Mapping[str, Any]) -> dict[str, Any]:
    def write(tx: Transaction) -> dict[str, Any]:
        conflict = {"status": "conflicted", "deleted_rows": 0, "upserted_rows": 0}
        rows = tx.execute("DECLARE $id AS Utf8; SELECT payload FROM reports VIEW by_id WHERE id=$id LIMIT 2;",
                          {"$id": action["report_id"]})[0].rows
        if len(rows) != 1 or rows[0].payload != action["expected_report_payload"]:
            return conflict
        rows = tx.execute("DECLARE $key AS Utf8; SELECT value FROM app_meta WHERE key=$key;",
                          {"$key": action["target_key"]})[0].rows
        current = rows[0].value if rows else None
        complete_target = current == action["after_target"]
        if not complete_target and current != action["expected_target"]:
            return conflict
        present = []
        for key, expected in action["sources"].items():
            rows = tx.execute("DECLARE $key AS Utf8; SELECT value FROM app_meta WHERE key=$key;",
                              {"$key": key})[0].rows
            if rows:
                if rows[0].value != expected:
                    return conflict
                present.append(key)
            elif not complete_target:
                return conflict
        if not complete_target:
            tx.execute("DECLARE $key AS Utf8; DECLARE $value AS Utf8; "
                       "UPSERT INTO app_meta (key,value) VALUES ($key,$value);",
                       {"$key": action["target_key"], "$value": action["after_target"]})
        for key in present:
            tx.execute("DECLARE $key AS Utf8; DELETE FROM app_meta WHERE key=$key;", {"$key": key})
        return {"status": "applied" if present or not complete_target else "already_complete",
                "deleted_rows": len(present), "upserted_rows": int(not complete_target)}

    return db.transaction(write)


def apply_chart_cleanup(db: YdbDatabase, plan: Mapping[str, Any]) -> dict[str, Any]:
    """Apply a reviewed private plan; concurrent mutations skip a whole report."""
    if plan.get("version") != _VERSION or not isinstance(plan.get("report_plans"), list):
        raise ValueError("unsupported chart cleanup plan")
    seen = set()
    for action in plan["report_plans"]:
        _validate_action(action)
        if action["report_id"] in seen:
            raise ValueError("duplicate planned report")
        seen.add(action["report_id"])
    counts: Counter[str] = Counter()
    for action in plan["report_plans"]:
        result = _apply_report(db, action)
        counts[result["status"] + "_reports"] += 1
        counts["deleted_rows"] += result["deleted_rows"]
        counts["upserted_rows"] += result["upserted_rows"]
    return {"report_plans": len(seen), **{key: counts[key] for key in (
        "applied_reports", "already_complete_reports", "conflicted_reports", "deleted_rows", "upserted_rows")}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("reports", type=Path)
    plan_parser.add_argument("metadata", type=Path)
    plan_parser.add_argument("output", type=Path)
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("plan", type=Path)
    args = parser.parse_args()
    if args.command == "plan":
        reports = [json.loads(line) for line in args.reports.read_text().splitlines() if line.strip()]
        metadata = [json.loads(line) for line in args.metadata.read_text().splitlines() if line.strip()]
        plan = plan_chart_cleanup(reports, metadata)
        # Exclusive creation prevents accidentally overwriting reviewed evidence.
        with open(args.output, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            json.dump(plan, stream, ensure_ascii=False)
        print(json.dumps(plan["summary"]))
    else:
        plan = json.loads(args.plan.read_text())
        db = YdbDatabase(YdbConfig.from_environment())
        try:
            print(json.dumps(apply_chart_cleanup(db, plan)))
        finally:
            db.close()


if __name__ == "__main__":
    main()
