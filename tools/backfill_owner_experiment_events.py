"""Bounded repair of owner event projections from unchanged journal records.

Run only in Docker with private YDB environment configuration. Dry-run is the
default; --apply writes projections and publication markers, never journals.
The optional result file contains private continuation identifiers and must stay
outside the repository and public logs. No schema initialization is performed.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import ydb  # type: ignore[import-untyped]

from zont_analyzer.adapters.ydb.database import Transaction, YdbConfig, YdbDatabase
from zont_analyzer.adapters.ydb.feedback import _micros, _text
from zont_analyzer.adapters.ydb.owner_experiments import _UNKNOWN_APPLIED_AT, OwnerExperimentsRepository


def repair_batch(db: YdbDatabase, *, limit: int = 100, after_id: str = "", apply: bool = False) -> dict[str, Any]:
    """Inspect one primary-key page, rereading each journal in its repair transaction."""
    if not 1 <= limit <= 100:
        raise ValueError("batch limit must be between 1 and 100")
    identifiers = db.transaction(lambda tx: [str(row.id) for row in tx.execute(
        "DECLARE $after AS Utf8; DECLARE $limit AS Uint64; "
        "SELECT id FROM owner_experiments WHERE id>$after ORDER BY id LIMIT $limit;",
        {"$after": after_id, "$limit": ydb.TypedValue(limit, ydb.PrimitiveType.Uint64)},
    )[0].rows])
    mismatched = 0
    for identifier in identifiers:
        def repair(tx: Transaction, identifier: str = identifier) -> bool:
            rows = tx.execute("DECLARE $id AS Utf8; SELECT payload FROM owner_experiments WHERE id=$id;",
                              {"$id": identifier})[0].rows
            if not rows:
                return False
            value = json.loads(_text(rows[0].payload) or "{}")
            desired = {}
            for event in [*OwnerExperimentsRepository._physical_events(value),
                          *OwnerExperimentsRepository._uncertain_events(value)]:
                boundary = event["temporal_boundary"]
                desired[event["intervention_id"]] = (
                    _micros(datetime.fromisoformat(boundary)) if boundary else _UNKNOWN_APPLIED_AT, event)
            params = {"$id": identifier, "$rollback": identifier + ":rollback",
                      "$undated": identifier + ":undated", "$rollback_undated": identifier + ":rollback-undated"}
            existing = tx.execute(
                "DECLARE $id AS Utf8; DECLARE $rollback AS Utf8; DECLARE $undated AS Utf8; "
                "DECLARE $rollback_undated AS Utf8; "
                "SELECT id,applied_at,payload FROM owner_experiment_events "
                "WHERE id IN AsList($id,$rollback,$undated,$rollback_undated);", params,
            )[0].rows
            actual = {str(row.id): (int(row.applied_at), json.loads(_text(row.payload) or "{}")) for row in existing}
            if actual == desired:
                return False
            if apply:
                OwnerExperimentsRepository._sync_events(tx, value)
                OwnerExperimentsRepository._publish_manual_change(tx, None, value)
            return True

        mismatched += int(db.transaction(repair))
    return {"scanned": len(identifiers), "mismatched": mismatched,
            "repaired": mismatched if apply else 0, "dry_run": not apply,
            "continuation_id": identifiers[-1] if len(identifiers) == limit else None}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--after-id", default=os.environ.get("ZONT_OWNER_EVENTS_AFTER_ID", ""))
    parser.add_argument("--private-result", type=Path,
                        help="new private JSON file outside the repository, including continuation cursor")
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    if args.private_result is not None:
        if args.private_result.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
            parser.error("--private-result must be outside the repository")
        if args.private_result.exists():
            parser.error("--private-result must name a new file")
    db = YdbDatabase(YdbConfig.from_environment())
    try:
        result = repair_batch(db, limit=args.limit, after_id=args.after_id, apply=args.apply)
    finally:
        db.close()
    if args.private_result is not None:
        with open(args.private_result, "x", encoding="utf-8",
                  opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            json.dump(result, stream, ensure_ascii=False)
    print(json.dumps({key: value for key, value in result.items() if key != "continuation_id"}
                     | {"has_more": result["continuation_id"] is not None}))


if __name__ == "__main__":
    main()
