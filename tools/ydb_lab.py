"""Reproducible fixture workloads for the isolated Docker YDB laboratory.

The accepted gas and application acceptance scenarios remain the source of their
business assertions. This harness adds complete RPC observations and paired runs;
it never connects to a managed database or an external source/AI provider.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import json
import os
import shutil
import sys
import time
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from tools.ydb_lab_observer import ObserveRpc, summarize
from tools.ydb_lab_queue import QueueClock, observe_publication_queue


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str, allow_nan=False).encode()).hexdigest()


class Harness:
    def __init__(self, args, years, repeat, suite):
        self.args, self.years, self.repeat, self.suite = args, years, repeat, suite
        self.runtime = None
        self.observer = None
        self.claimed = set()
        self.queue_clock = QueueClock()

    def emit(self, record):
        record = {"variant": self.args.variant, "source_sha": self.args.source_sha,
                  "years": self.years, "repeat": self.repeat, "suite": self.suite, **record}
        with (self.args.output / "results.jsonl").open("a") as stream:
            stream.write(json.dumps(record, sort_keys=True, default=str, allow_nan=False) + "\n")
        from tools.ydb_lab_report import compact_record
        with (self.args.output / "measurements.jsonl").open("a") as stream:
            stream.write(json.dumps(compact_record(record), sort_keys=True,
                                    default=str, allow_nan=False) + "\n")

    def measure(self, name, operation, *, phase="operation", expected=None):
        result, failure = None, None
        observer = self.observer
        first = len(observer.records)
        previous = observer.collect_stats
        observer.collect_stats = phase != "setup"
        start = time.monotonic()
        try:
            result = operation()
        except Exception as exc:
            failure = exc
        finally:
            observer.collect_stats = previous
        elapsed = time.monotonic() - start
        events = copy.deepcopy(observer.records[first:])
        self.claimed.update(range(first, first + len(events)))
        offset = start - observer._start
        for record in events:
            record["started_seconds"] -= offset
            if record["completed_seconds"] is not None:
                record["completed_seconds"] -= offset
        elapsed = max([elapsed, *(r.get("completed_seconds") or 0 for r in events)])
        observation = summarize(events)
        pending = result.get("pending_reports") if isinstance(result, dict) else None
        self.emit({"kind": "operation", "scenario": name, "phase": phase,
                   "elapsed_seconds": elapsed, "statistics_mode": "default" if phase == "setup" else "basic",
                   "observation": observation, "events": events,
                   "expected_error": expected is not None, "error": type(failure).__name__ if failure else None,
                   "result_sha256": digest(result), "queue_depth": pending,
                   "queue_age_clock": "monotonic",
                   "queue_age_seconds": self.queue_clock.age(pending) if type(pending) is int else None})
        print(f"{self.args.variant} {self.years}y repeat {self.repeat} {name}: "
              f"{observation['measured_ru']} RU, {elapsed:.2f}s", flush=True)
        if failure is not None and (expected is None or not isinstance(failure, expected)):
            raise failure
        if expected is not None and failure is None:
            raise AssertionError(f"{name}: expected failure not exercised")
        return result

    def finish(self):
        # Include setup, validation reads and session lifecycle outside named operations.
        events = [event for i, event in enumerate(self.observer.records) if i not in self.claimed]
        self.emit({"kind": "operation", "scenario": "setup_and_validation", "phase": "setup",
                   "elapsed_seconds": time.monotonic() - self.observer._start,
                   "statistics_mode": "default", "observation": summarize(events), "events": events,
                   "expected_error": False, "error": None, "queue_depth": None, "queue_age_seconds": None,
                   "exclusions": ["five_unobserved_overhead_control_reads"]})

    def capture_runtime(self, loaded, db):
        from zont_analyzer.runtime import Runtime
        if self.suite == "application":
            # The path is part of the application's settings hash. Use the same
            # isolated container-local path and fresh contents in every run.
            loaded.data_dir = Path("/tmp/ydb-lab-application")
            shutil.rmtree(loaded.data_dir, ignore_errors=True)
            loaded.config.pilot.reports_dir = str(loaded.data_dir / "published")
        self.runtime = Runtime(loaded, db)
        return self.runtime

    def capture_storage(self, config):
        from zont_analyzer.adapters.ydb.database import YdbDatabase
        from zont_analyzer.adapters.ydb.schema import TABLES
        db = YdbDatabase(config)
        initialize = db.initialize
        original_close = db.close
        db.initialize = lambda: self.measure("schema_setup", initialize, phase="setup")

        def cleanup():
            for name in TABLES:
                db.execute(f"DROP TABLE IF EXISTS `{name}`;", timeout_seconds=5)
            db.driver.scheme_client.remove_directory(db.path)

        def close():
            already_failing = sys.exc_info()[0] is not None
            try:
                self.measure("schema_cleanup", cleanup, phase="setup")
            except Exception:
                if not already_failing:
                    raise
            finally:
                original_close()

        db.close = close
        return db

    def validate(self, facts, artifacts, ai):
        self.emit({"kind": "validation", "facts_sha256": digest(facts),
                   "artifacts_sha256": digest(artifacts), "ai_sha256": digest(ai)})
        # Raw synthetic artifacts stay outside the public repository for diagnosis.
        name = f"{self.args.variant}-{self.years}-{self.repeat}-{self.suite}-facts.json"
        (self.args.output / name).write_text(json.dumps(
            {"facts": facts, "artifacts": artifacts, "ai": ai}, sort_keys=True, default=str))


@contextmanager
def fixed_inputs(now):
    """Fix only fixture identity/wall-clock metadata, never the SDK or lease clock."""
    from zont_analyzer.adapters.ydb import application, owner, owner_data, telemetry
    from zont_analyzer.application import owner_context

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    identities = itertools.count(1)
    with ExitStack() as stack:
        for module in (application, owner, telemetry):
            stack.enter_context(patch.object(module, "datetime", FixedDatetime))
        stack.enter_context(patch.object(owner_context, "utcnow", lambda: now))
        stack.enter_context(patch.object(owner_data, "_now", lambda: int(now.timestamp() * 1e6)))
        stack.enter_context(patch.object(owner_data, "uuid4", lambda: UUID(int=next(identities))))
        yield


def artifacts_at(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.suffix not in {".html", ".json"}:
            continue
        value = path.read_text()
        if path.suffix == ".json":
            value = json.loads(value)
            if path.name == "reports.json":
                for entry in value["reports"]:
                    entry.pop("published_at", None)  # filesystem mtime only
        result[path.relative_to(root).as_posix()] = value
    return result


def gas_suite(h):
    from tools import benchmark_gas_updates as workload
    from zont_analyzer.application import publication
    from zont_analyzer.application.gas import GasService
    from zont_analyzer.application.owner_context import OwnerContextStore
    from zont_analyzer.application.pilot import reports_directory
    from zont_analyzer.domain import TelemetryPoint

    def record(storage, years, label, operation, output):
        return h.measure(label, operation)

    def complete(record, output=None):
        if record.get("operation") != "acceptance":
            return
        runtime, db = h.runtime, h.runtime.db
        original = sorted(db._all_reports(), key=lambda r: r.id)
        ai_before = [(r.id, r.summary, r.ai_used, r.context.get("ai_provenance")) for r in original]
        anchor = next(r for r in original if r.kind == "daily")
        first = datetime(2026 - h.years, 1, 1, tzinfo=UTC)

        def drain():
            rendered = 0
            for _ in range(len(original) + 4):
                state = publication.publish_reports(runtime, now=workload.NOW, batch_size=16)
                rendered += state["rendered_reports"]
                if not state["pending_reports"]:
                    return {**state, "rendered_reports": rendered}
            raise AssertionError("publication did not drain")

        h.measure("gas_reading_correction", lambda: OwnerContextStore(db).update_gas(anchor.id, {
            "day": (first + timedelta(days=24)).date().isoformat(), "value_m3": 760}))
        h.measure("gas_corrected_publication", drain)
        point = TelemetryPoint(device_id="1", entity_id="boiler", source_type="z3k_boiler_adapter",
                               metric_key="rml", timestamp_utc=first + timedelta(days=20, hours=1), value_num=20)
        h.measure("late_modulation", lambda: db.telemetry.write_window(
            device_id="1", data_type="dense-calibration", start=point.timestamp_utc,
            end=point.timestamp_utc + timedelta(seconds=1), points=[point]))
        h.measure("late_model_refresh", lambda: GasService(db, runtime.config).model()[0])
        h.measure("late_publication", drain)
        current = sorted(db._all_reports(), key=lambda r: r.id)
        ai_after = [(r.id, r.summary, r.ai_used, r.context.get("ai_provenance")) for r in current]
        assert ai_before == ai_after, "gas correction or late modulation changed stored AI"
        h.validate([r.model_dump(mode="json") for r in current], artifacts_at(reports_directory(runtime)), ai_after)

    args = SimpleNamespace(endpoint="grpc://ydb:2136", database="/local",
                           namespace=f"{h.args.namespace}_g{h.repeat}", case_year=h.years, batch_size=16, output=None)
    with ExitStack() as stack:
        stack.enter_context(fixed_inputs(workload.NOW))
        stack.enter_context(patch.object(workload, "measured", record))
        stack.enter_context(patch.object(workload, "emit", complete))
        stack.enter_context(patch.object(workload, "Runtime", h.capture_runtime))
        stack.enter_context(patch.object(workload, "YdbDatabase", h.capture_storage))
        for name in ("_seed", "seed_dense"):
            original = getattr(workload, name)
            stack.enter_context(patch.object(workload, name,
                lambda *a, _fn=original, _name=name, **kw: h.measure(
                    "seed" + _name, lambda: _fn(*a, **kw), phase="setup")))
        workload.run_case(args)


def application_probes(h):
    """Additional real adapter, idle-job and interruption scenarios on the same history."""
    import ydb

    from tools import benchmark_operations_cost as workload
    from zont_analyzer.application import analysis, publication
    from zont_analyzer.cloud import monitoring, user_jobs
    from zont_analyzer.cloud.report_jobs import ReportJobRunner
    from zont_analyzer.cloud.scheduler import ProductionScheduler
    from zont_analyzer.domain import TelemetryPoint
    from zont_analyzer.domain.periods import Period

    runtime, db = h.runtime, h.runtime.db
    storage = db.storage
    h.measure("control_write", lambda: db.set_app_meta("lab-control", "value"))
    for cache in ("cold", "warm"):
        h.measure("control_read_" + cache, lambda: db.get_app_meta("lab-control"))
        h.measure("control_cpu_" + cache, lambda: storage.execute(
            "SELECT SUM(value_num * value_num) AS total FROM telemetry_samples;"))
    h.measure("telemetry_bounds", lambda: (db.earliest_sample_time(), db.latest_sample_time()))
    h.measure("control_commit", lambda: storage.transaction(
        lambda tx: tx.execute("SELECT value FROM app_meta WHERE key='lab-control';")))
    h.measure("control_read_table", lambda: storage.read_table(
        "app_meta", columns=["key", "value"], key_range=None, consume=lambda rows: sum(1 for _ in rows)))
    h.measure("monitoring", lambda: monitoring._snapshot(runtime))
    h.measure("maintenance_idle", lambda: user_jobs.drain(runtime))

    # All timer lanes are not due; retain the actual scheduler reads/checkpoints.
    now = workload.NOW
    future = (now + timedelta(days=1)).isoformat()
    db.set_app_meta("source-event-report-baselines:v1:complete", "1")
    lease = db.jobs.acquire("production-scheduler:v1", "fixture", 30)
    state = {"next_sync": future, "next_recommendation_maintenance": future}
    db.jobs.checkpoint(lease.job_key, lease.owner, lease.attempt, json.dumps(state))
    db.jobs.release(lease.job_key, lease.owner, lease.attempt)
    scheduler = ProductionScheduler(runtime, now=lambda: now)
    # Suppress due-work selection only; the idle DB path and lock remain real.
    with patch.object(scheduler, "_reports", lambda *a: None), \
         patch.object(user_jobs, "scheduled_review", lambda *a, **kw: {"status": "not_due"}):
        assert h.measure("scheduler_idle", scheduler.run)["status"] == "idle"

    # A real server error followed by a successful retry is fully observable.
    h.measure("query_error", lambda: storage.execute("SELECT * FROM lab_missing_table;"), expected=ydb.Error)
    assert h.measure("query_recovery", lambda: db.get_app_meta("lab-control")) == "value"

    # The adapter retries a transaction callback after ABORTED, including its first query.
    calls = 0

    def retry(tx):
        nonlocal calls
        calls += 1
        tx.execute("SELECT value FROM app_meta WHERE key='lab-control';")
        if calls == 1:
            raise ydb.Aborted("fixture retry after successful read")
        return calls

    assert h.measure("transaction_retry", lambda: storage.transaction(retry)) == 2

    # Idle observer wall/CPU cost, and identical control reads without/with instrumentation.
    for observed in (False, True):
        before_cpu, before_wall = time.process_time(), time.perf_counter()
        if observed:
            h.measure("observer_control", lambda: [db.get_app_meta("lab-control") for _ in range(5)], phase="observer")
        else:
            h.observer.enabled = False
            try:
                for _ in range(5):
                    db.get_app_meta("lab-control")
            finally:
                h.observer.enabled = True
        h.emit({"kind": "overhead", "observed": observed,
                "cpu_seconds": time.process_time() - before_cpu,
                "elapsed_seconds": time.perf_counter() - before_wall, "queries": 5})
    h.measure("observer_idle", lambda: time.sleep(0.02), phase="observer")

    # New/replayed/late samples use the same transactional ingestion repository.
    point = TelemetryPoint(device_id="1", entity_id="outdoor", source_type="z3k_boiler_adapter",
                           metric_key="temperature", timestamp_utc=now - timedelta(minutes=10), value_num=-4)

    def write(p):
        return db.telemetry.write_window(device_id="1", data_type="temperature", start=p.timestamp_utc,
                                          end=p.timestamp_utc + timedelta(seconds=1), points=[p],
                                          roles={"outdoor": "outdoor_temperature"})

    h.measure("new_sample", lambda: write(point))
    revision = db.source_revision()
    h.measure("identical_sample", lambda: write(point))
    assert db.source_revision() == revision
    h.measure("late_sample", lambda: write(point.model_copy(update={
        "timestamp_utc": now - timedelta(days=1, hours=3), "value_num": -5})))

    # New long-period reports on archived coverage, using fixture-only collection.
    client = workload.FixtureClient()
    runner = ReportJobRunner(runtime, client_factory=lambda: client, now=lambda: now, publish=False)
    start = now.replace(month=6, day=1, hour=0)
    end = now.replace(month=9, day=1, hour=0)
    for kind in ("temperature", "raw_events"):
        db.telemetry.write_window(device_id="1", data_type=kind, start=start, end=end, state="empty")

    def finish(operation):
        for _ in range(10):
            result = operation()
            if result["status"] == "done":
                return {"status": "done", "report_id": result["report_id"]}
        raise AssertionError("report phases did not finish")

    with patch.object(analysis, "datetime", workload.FixedDatetime):
        monthly = {"kind": "monthly", "year": 2026, "month": 8, "use_ai": False, "max_requests": 8}
        h.measure("monthly_report", lambda: finish(lambda: runner.run(monthly)))
        period = Period(kind="seasonal", start=start, end=end, observed_end=end,
                        timezone="UTC", complete=True, season="summer", year=2026)
        h.measure("seasonal_report", lambda: finish(lambda: runner.run_scheduled(period, use_ai=False)))

        # Save a real report, lose ownership at completion, then resume its checkpoint.
        payload = {"kind": "daily", "date": "2026-09-19", "use_ai": False, "max_requests": 8}
        for kind in ("temperature", "raw_events"):
            db.telemetry.write_window(device_id="1", data_type=kind,
                start=datetime(2026, 9, 19, tzinfo=UTC), end=datetime(2026, 9, 20, tzinfo=UTC), state="empty")
        assert h.measure("restart_collect", lambda: runner.run(payload))["phase"] == "analyze"
        with patch.object(runner.jobs, "complete", lambda *a: False):
            h.measure("lease_lost_after_save", lambda: runner.run(payload), expected=RuntimeError)
        resumed = ReportJobRunner(runtime, client_factory=lambda: client, now=lambda: now, publish=False)
        original_analysis = runtime.analysis

        def forbid_reanalysis(*a, **kw):
            if kw.get("job_fence") is not None:
                raise AssertionError("saved report analyzed again")
            return original_analysis(*a, **kw)

        with patch.object(runtime, "analysis", forbid_reanalysis):
            assert h.measure("restart_saved_report", lambda: resumed.run(payload))["status"] == "done"
    h.measure("final_publication", lambda: publication.publish_reports(runtime, now=now, batch_size=16))


def application_suite(h):
    from tools import benchmark_operations_cost as workload
    from zont_analyzer.application.pilot import reports_directory

    seed = workload._seed
    def measure(label, operation, args, *, expect_failure=False):
        return h.measure(label, operation, expected=RuntimeError if expect_failure else None)

    def history(db, start, end):
        return h.measure("history_seed", lambda: seed(db, start.replace(year=start.year - h.years), end), phase="setup")

    args = SimpleNamespace(label=h.args.variant, source_sha=h.args.source_sha,
        namespace=f"{h.args.namespace}_a{h.years}_{h.repeat}", days=3,
        output=str(h.args.output / f"{h.args.variant}-{h.years}-{h.repeat}-application-original.json"))
    cleanup = []

    def storage(config):
        db = h.capture_storage(config)
        cleanup.append(db.close)
        db.close = lambda: None
        return db

    with fixed_inputs(workload.NOW), patch.object(workload, "_seed", history), \
         patch.object(workload, "Runtime", h.capture_runtime), \
         patch.object(workload, "YdbDatabase", storage), \
         patch.object(workload, "measure", measure):
        try:
            workload.run(args)
            application_probes(h)
            reports = sorted(h.runtime.db._all_reports(), key=lambda r: r.id)
            h.validate([r.model_dump(mode="json") for r in reports], artifacts_at(reports_directory(h.runtime)),
                       [(r.id, r.summary, r.ai_used) for r in reports])
        finally:
            for close in cleanup:
                close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=["run", "summarize", "serve"], default="run")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--variant", choices=["baseline", "candidate"])
    parser.add_argument("--source-sha")
    parser.add_argument("--namespace", default="lab")
    parser.add_argument("--years", nargs="+", type=int, choices=[1, 5, 10], default=[1, 5, 10])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--suites", nargs="+", choices=["gas", "application"], default=["application", "gas"])
    args = parser.parse_args()
    if args.command != "run":
        from tools.ydb_lab_report import serve, summarize_output
        if args.command == "serve":
            serve(args.output, args.port)
        else:
            summary = summarize_output(args.output)
            print(json.dumps({key: summary[key] for key in ("validation_passed", "accounting_complete")}))
        return
    if not args.variant or not args.source_sha or not 1 <= args.repeats <= 10:
        parser.error("run requires variant, source SHA and 1..10 repeats")
    if any(os.environ.get(name) for name in ("OPENAI_API_KEY", "ZONT_TOKEN", "CLOUD_PUBLICATION_BUCKET")):
        parser.error("lab forbids production credentials and cloud publication")
    # Anonymous hard-coded local endpoint; no provider configuration is read.
    for years in args.years:
        for repeat in range(1, args.repeats + 1):
            for suite in args.suites:
                h = Harness(args, years, repeat, suite)
                with ObserveRpc(collect_stats=False) as h.observer, observe_publication_queue(h.queue_clock):
                    try:
                        (application_suite if suite == "application" else gas_suite)(h)
                    finally:
                        h.finish()


if __name__ == "__main__":
    main()
