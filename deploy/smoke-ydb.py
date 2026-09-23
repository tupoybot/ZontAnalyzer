"""Bounded storage smoke for an explicitly isolated YDB namespace, without external APIs."""

from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from zont_analyzer.adapters.ydb.ai import AiResponseCache, ModelReviewRunRepository
from zont_analyzer.adapters.ydb.database import YdbConfig, YdbDatabase
from zont_analyzer.adapters.ydb.jobs import JobLeaseRepository, UsageLedger
from zont_analyzer.adapters.ydb.owner import OwnerRepository
from zont_analyzer.adapters.ydb.reports import ReportRepository
from zont_analyzer.adapters.ydb.telemetry import TelemetryRepository
from zont_analyzer.domain import QualityResult, Report, SourceEvent, TelemetryPoint


def main() -> None:
    db = YdbDatabase(YdbConfig.from_environment())
    try:
        db.initialize()
        key = "smoke-" + uuid.uuid4().hex
        start = datetime(2026, 1, 1, tzinfo=UTC)
        telemetry = TelemetryRepository(db)
        begin = time.monotonic()
        telemetry.save_devices([{"id": key}])
        for _ in range(2):
            telemetry.write_window(
                device_id=key, data_type="history", start=start, end=start + timedelta(hours=1),
                points=[TelemetryPoint(device_id=key, source_type="fixture", entity_id="room", metric_key="t",
                                       timestamp_utc=start, value_num=20.5)],
                events=[SourceEvent(id=key, device_id=key, event_type="fixture", timestamp_utc=start)],
            )
        snapshot = telemetry.read_period(key, start, start + timedelta(hours=1))
        assert len(snapshot["samples"]) == len(snapshot["events"]) == 1
        jobs = JobLeaseRepository(db)
        with ThreadPoolExecutor(max_workers=2) as workers:
            leases = list(workers.map(lambda owner: jobs.acquire(key, owner, 60), ("a", "b")))
        assert sum(lease is not None for lease in leases) == 1
        usage = UsageLedger(db)
        usage.prepare(key, key, "{}")
        assert usage.mark_sent(key)
        assert not usage.mark_sent(key)
        usage.mark_unknown(key, "{}")
        assert not usage.mark_sent(key)
        owner = OwnerRepository(db)
        owner.save_gas_reading(key, "2026-01-01", "123.4500", expected_version=0)
        assert owner.gas_reading(key, "2026-01-01")["value_m3"] == "123.4500"
        owner.save_tariff(key, "2026-01", "1.2300", "RUB", expected_version=0)
        assert owner.tariff_history(key)[0]["price"] == "1.2300"
        cache = AiResponseCache(db)
        cache.put_success(key, '{"answer":"fixture"}', '{"provider":"mock"}', "legacy", "historical-model")
        assert cache.get(key).model == "historical-model"
        review = ModelReviewRunRepository(db)
        review.save_state(key, '{"status":"idle"}', expected_version=0)
        assert review.get_state(key).version == 1
        assert owner.save_ai_settings({"model": "fixture"}, expected_version=0, scope=key) == 1
        report = Report(
            id=key, kind="daily", period_start=start, period_end=start + timedelta(days=1), generated_at=start,
            quality=QualityResult(score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
                                  implausible_jumps=0, sample_count=1), summary="storage fixture",
            algorithm_version=key,
        )
        reports = ReportRepository(db)
        assert reports.save_report(report, "fixture", telemetry_scope="telemetry:" + key,
                                   telemetry_revision=snapshot["revision"]) == 1
        assert reports.save_report(report, "fixture") == 1
        assert reports.report(key).summary == report.summary
        print(json.dumps({"status": "ok", "seconds": time.monotonic() - begin, "checks": [
            "telemetry_repeat", "events", "snapshot", "lease_concurrency", "unknown_llm_outcome",
            "gas_decimal", "tariff_decimal", "report_atomic_save_and_repeat",
            "ai_cache_provenance", "model_review_state", "ai_settings_version",
        ]}))
    finally:
        db.close()


if __name__ == "__main__":
    main()
