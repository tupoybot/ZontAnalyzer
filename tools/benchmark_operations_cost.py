"""Bounded application operations on disposable YDB; no external source or AI calls.

Invoked by deploy/measure-operations-cost.sh. Both revisions run unchanged source
against identical synthetic input. Native RU are local server observations, not
managed YDB billing. Initialization, seeding and validation reads are excluded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from tools.benchmark_ydb_cost import _seed
from tools.measure_ydb_bounds import ObserveRpc
from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.adapters.ydb.database import YdbConfig, YdbDatabase
from zont_analyzer.application import analysis, publication
from zont_analyzer.application.collection import CollectionService
from zont_analyzer.application.pilot import reports_directory
from zont_analyzer.cloud.report_jobs import ReportJobRunner
from zont_analyzer.config import AppConfig, LoadedConfig, Secrets
from zont_analyzer.runtime import Runtime

NOW = datetime(2026, 9, 24, 2, tzinfo=UTC)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class FixtureClient:
    """Local empty responses for gaps; telemetry itself is seeded before timing."""
    def __init__(self):
        self.calls = 0

    def discover_devices(self):
        return [{"id": "1"}]

    def load_history(self, **kwargs):
        self.calls += 1
        return [{"device_id": "1", "ok": True}]

    def normalize_history(self, response):
        return [], {}

    def load_events(self, **kwargs):
        self.calls += 1
        return []

    def normalize_events(self, device_id, rows):
        return []

    def close(self):
        pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def measure(label, operation, args, *, expect_failure=False):
    error = None
    result = None
    with ObserveRpc() as observed:
        begun = time.perf_counter()
        try:
            result = operation()
        except RuntimeError as exc:
            error = str(exc)
            if not expect_failure or error != "injected publication interruption":
                raise
        elapsed = time.perf_counter() - begun
    native = observed.summary()
    record = {"label": args.label, "source_sha": args.source_sha,
              "operation": label, "elapsed_seconds": round(elapsed, 4),
              "result": result, "error": error, "native": native}
    print(json.dumps(record, sort_keys=True, default=str), flush=True)
    if expect_failure and error is None:
        raise AssertionError("publication failure was not exercised")
    if not native["all_rpc_complete"] or native["ru_missing_rpc_count"]:
        raise AssertionError("incomplete native RPC accounting")
    return result


def run(args):
    expected_source = Path("/artifacts/baseline/src" if args.label == "baseline" else "/workspace/src")
    assert Path(analysis.__file__).is_relative_to(expected_source), "wrong application revision loaded"
    storage = YdbDatabase(YdbConfig("grpc://ydb:2136", "/local", args.namespace, True))
    try:
        storage.initialize()
        db = Database(storage)
        config = AppConfig()
        config.home.timezone = "UTC"
        config.zont.history_data_types = ["temperature"]
        runtime = Runtime(LoadedConfig(config=config, secrets=Secrets(), config_path=None,
                                      data_dir=Path("/tmp/operations"), sources={}), db)
        start = NOW.replace(hour=0) - timedelta(days=args.days)
        points = _seed(db, start, NOW.replace(hour=0))
        # Imported archive plus one hour of missing tail, as in a normal sync.
        # A fresh multi-day source backfill would exceed the bounded request budget.
        for data_type in ("temperature", "raw_events"):
            db.telemetry.write_window(device_id="1", data_type=data_type, start=start,
                                      end=NOW.replace(hour=0) - timedelta(hours=1), state="empty")
        client = FixtureClient()
        runner = ReportJobRunner(runtime, client_factory=lambda: client, now=lambda: NOW, publish=False)
        payloads = [{"kind": "daily", "date": (start + timedelta(days=i)).date().isoformat(),
                     "use_ai": False, "max_requests": 8} for i in range(args.days)]
        collection = CollectionService(db, client, config)

        def sync():
            return collection.ensure_period(start, NOW.replace(hour=0), now=NOW, max_requests=24)

        result = measure("sync_cold", sync, args)
        assert result["complete"]
        for index in range(3):
            calls = client.calls
            result = measure(f"sync_idle_{index + 1}", sync, args)
            assert result["complete"] and client.calls == calls

        def calculate():
            completed = []
            for payload in payloads:
                phases = []
                for _ in range(8):
                    result = runner.run(payload)
                    phases.append(result)
                    if result["status"] == "done":
                        break
                else:
                    raise AssertionError("bounded report phases did not finish")
                completed.append(phases)
            return {"completed_reports": len(completed), "phases": completed,
                    "fixture_source_calls": client.calls}

        with patch.object(analysis, "datetime", FixedDatetime):
            measure("report_calculation_cold", calculate, args)
            for index in range(3):
                results = measure(f"report_freshness_warm_{index + 1}",
                                  lambda: [runner.run(p) for p in payloads], args)
                assert all(r["reused"] and r["status"] == "done" for r in results)

        def publish():
            return publication.publish_reports(runtime, now=NOW, batch_size=2)

        first = measure("publication_cold_batch", publish, args)
        assert first["rendered_reports"] == 2
        latest_before_failure = (reports_directory(runtime) / "latest.html").read_bytes()
        original = publication._write_changed

        def interrupt(path, content):
            if path.suffix == ".html":
                raise RuntimeError("injected publication interruption")
            return original(path, content)

        with patch.object(publication, "_write_changed", interrupt):
            measure("publication_failed_batch", publish, args, expect_failure=True)
        assert (reports_directory(runtime) / "latest.html").read_bytes() == latest_before_failure
        for index in range(args.days):
            result = measure(f"publication_retry_batch_{index + 1}", publish, args)
            if result["pending_reports"] == 0:
                break
        else:
            raise AssertionError("publication retry did not drain")
        for index in range(3):
            result = measure(f"publication_idle_retry_{index + 1}", publish, args)
            assert result["rendered_reports"] == result["pending_reports"] == 0
        reports = [db.report(runner.runtime.analysis(no_ai=True).report_id_for(
            "daily", start + timedelta(days=i))).model_dump(mode="json") for i in range(args.days)]
        output = reports_directory(runtime)
        artifacts = {}
        for path in sorted(output.rglob("*")):
            if path.suffix not in {".html", ".json"}:
                continue
            body = path.read_text()
            if path.name == "reports.json":
                manifest = json.loads(body)
                for entry in manifest["reports"]:
                    entry.pop("published_at", None)  # filesystem mtime, explicitly not semantic
                artifacts[path.relative_to(output).as_posix()] = manifest
            elif path.suffix == ".json":
                artifacts[path.relative_to(output).as_posix()] = json.loads(body)
            else:
                artifacts[path.relative_to(output).as_posix()] = body
        assert len([key for key in artifacts if key.startswith("daily/") and key.endswith(".json")]) == args.days
        evidence = {"reports": reports, "artifacts": artifacts}
        Path(args.output).write_text(json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2))
        print(json.dumps({"label": args.label, "operation": "validation", "days": args.days,
                          "seed_points": points, "completed_reports": len(reports),
                          "sync_fixture": "archived coverage with a one-hour missing tail",
                          "canonical_sha256": digest(reports), "artifacts_sha256": digest(artifacts),
                          "normalization": "manifest published_at filesystem mtime only",
                          "fixture_source_calls": client.calls}, sort_keys=True), flush=True)
    finally:
        storage.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", choices=["baseline", "candidate"], required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--days", type=int, choices=range(3, 11), default=3)
    parser.add_argument("--output", required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
