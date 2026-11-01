"""Compare compact publication queue work with PR #62's exact repository code.

Run in the local Docker test image against disposable YDB. Extract the baseline
file with `git show 3149665:src/zont_analyzer/adapters/ydb/publication.py` and
mount it read-only at --baseline-publication. This measures queue selection and
an eight-item save, not report rendering or managed-cloud request units.
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tools.benchmark_ydb_cost import ObserveQueries
from zont_analyzer.adapters.ydb import publication as publication_module
from zont_analyzer.adapters.ydb.database import YdbConfig, YdbDatabase
from zont_analyzer.adapters.ydb.jobs import JobLeaseRepository
from zont_analyzer.adapters.ydb.publication import PublicationRepository
from zont_analyzer.application.incremental_publication import _queue


def _baseline_methods(path: Path) -> tuple[Any, Any]:
    tree = ast.parse(path.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == "PublicationRepository")
    methods: list[ast.stmt] = [node for node in cls.body if isinstance(node, ast.FunctionDef)
                               and node.name in {"load", "save"}]
    if len(methods) != 2:
        raise ValueError("baseline publication methods missing")
    scope = vars(publication_module).copy()
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(path), "exec"), scope)
    return scope["load"], scope["save"]


def _item(index: int, total: int) -> dict[str, Any]:
    day = datetime(2015, 1, 1, tzinfo=UTC) + timedelta(days=index)
    start = int(day.timestamp())
    href = f"daily/{day:%Y/%m/%d}.html"
    return {
        "href": href, "report_id": f"report-{index:05d}", "kind": "daily",
        "start": start, "end": start + 86_400, "generated": (start + 86_400) * 1_000_000,
        "digest": f"digest-{index:05d}", "lo": float(start - 900),
        "hi": float(start + 86_400 + 900), "comparisons": False,
        "dirty": 1 if index % 10 == 0 or index == total - 1 else 0,
        "queued": index + 1, "entry": json.dumps({"href": href}),
        "json_stamp": "stamp-json", "html_stamp": "stamp-html",
    }


def _record(years: int, run: str, operation: str, seconds: float, costs: Any,
            selected: list[str], pending_count: int) -> None:
    print(json.dumps({
        "years": years, "run": run, "operation": operation,
        "elapsed_seconds": round(seconds, 3), "queries": costs.queries,
        "result_rows": costs.result_rows, "result_json_bytes": costs.result_json_bytes,
        "sdk_read_rows": costs.sdk_read_rows, "sdk_read_bytes": costs.sdk_read_bytes,
        "sdk_cpu_us": costs.sdk_cpu_us, "queries_with_stats": costs.queries_with_stats,
        "pending_count": pending_count,
        "selected_sha256": hashlib.sha256(json.dumps(selected).encode()).hexdigest(),
    }, sort_keys=True), flush=True)


def _measure(storage: YdbDatabase, action: Any) -> tuple[Any, float, Any]:
    with ObserveQueries(storage) as costs:
        start = time.perf_counter()
        result = action()
        seconds = time.perf_counter() - start
    return result, seconds, costs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-publication", type=Path, required=True)
    parser.add_argument("--endpoint", default="grpc://ydb:2136")
    parser.add_argument("--database", default="/local")
    parser.add_argument("--namespace", default="queue_cost_benchmark")
    args = parser.parse_args()
    if not args.endpoint.startswith("grpc://") or args.database != "/local":
        parser.error("queue seeding requires anonymous grpc:// YDB at /local")
    baseline_load, baseline_save = _baseline_methods(args.baseline_publication)
    storage = YdbDatabase(YdbConfig(args.endpoint, args.database, args.namespace, True))
    try:
        storage.initialize()
        repository = PublicationRepository(storage)
        lease = JobLeaseRepository(storage).acquire("publication", "benchmark", 3_600)
        if lease is None:
            raise RuntimeError("publication benchmark lease unavailable")
        print(json.dumps({"baseline_commit": "3149665", "storage": "local YDB emulator",
                          "baseline_sha256": hashlib.sha256(args.baseline_publication.read_bytes()).hexdigest(),
                          "batch_size": 8, "dirty_fraction": "one in ten plus latest",
                          "unpublished_count": 0}), flush=True)
        seeded = 0
        for years in (1, 5, 10):
            total = 365 * years
            additions = [_item(index, total) for index in range(seeded, total)]
            added = {item["href"]: item for item in additions}
            # The previous latest was dirty in the shorter dataset. It remains
            # dirty after expansion, giving both implementations identical input.
            if not repository.save(added, {}, {}, {}, expected_checkpoint=0,
                                   lease_owner="benchmark", lease_attempt=lease.attempt):
                raise RuntimeError("queue seed was not saved")
            seeded = total
            latest = _item(total - 1, total)["href"]

            def old_read(latest_href: str = latest) -> tuple[list[str], int, dict[str, dict[str, Any]]]:
                items, _ = baseline_load(repository)
                selected = [item["href"] for item in _queue(items, latest_href, 8)]
                return selected, sum(bool(item["dirty"]) for item in items.values()), items

            def new_read(latest_href: str = latest) -> tuple[list[str], int, dict[str, dict[str, Any]]]:
                items = repository.pending_items(8, latest_href, include_unpublished=False)
                selected = [item["href"] for item in _queue(items, latest_href, 8)]
                return selected, repository.pending_count()[0], items

            old, old_seconds, old_costs = _measure(storage, old_read)
            new, new_seconds, new_costs = _measure(storage, new_read)
            if old[:2] != new[:2]:
                raise AssertionError("queue selection or pending count changed")
            _record(years, "baseline", "read", old_seconds, old_costs, old[0], old[1])
            _record(years, "candidate", "read", new_seconds, new_costs, new[0], new[1])

            previous = {href: old[2][href] for href in old[0]}
            changed = copy.deepcopy(previous)
            for item in changed.values():
                item["digest"] += ":updated"
            save_args: tuple[Any, ...] = (changed, previous, {}, {})
            kwargs = {"expected_checkpoint": 0, "lease_owner": "benchmark",
                      "lease_attempt": lease.attempt}
            old_saved, old_seconds, old_costs = _measure(
                storage, lambda args=save_args, options=kwargs: baseline_save(repository, *args, **options))
            new_saved, new_seconds, new_costs = _measure(
                storage, lambda args=save_args, options=kwargs: repository.save(*args, **options))
            if not old_saved or not new_saved:
                raise AssertionError("queue save was not fenced successfully")
            _record(years, "baseline", "save", old_seconds, old_costs, old[0], old[1])
            _record(years, "candidate", "save", new_seconds, new_costs, old[0], old[1])
            for href, item in changed.items():
                rows = storage.execute("DECLARE $href AS Utf8; SELECT digest FROM publication_items WHERE href=$href;",
                                       {"$href": href})[0].rows
                if str(rows[0].digest) != item["digest"]:
                    raise AssertionError("saved queue digest differs")
    finally:
        storage.close()


if __name__ == "__main__":
    main()
