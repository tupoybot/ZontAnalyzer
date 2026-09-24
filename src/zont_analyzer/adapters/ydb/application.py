"""Application operations backed exclusively by native YDB repositories."""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from zont_analyzer.domain import Report, SourceEvent

from .ai_usage import AiUsageRepository
from .database import Transaction, YdbConfig, YdbDatabase
from .feedback import FeedbackRepository
from .jobs import JobLeaseRepository, UsageLedger
from .owner import OwnerRepository
from .reports import ReportRepository
from .telemetry import TelemetryRepository, bump_revision, encode, utc_seconds


class Database:
    def __init__(self, config: YdbConfig | YdbDatabase) -> None:
        self.storage = config if isinstance(config, YdbDatabase) else YdbDatabase(config)
        self.telemetry = TelemetryRepository(self.storage)
        self.owner = OwnerRepository(self.storage)
        self.reports = ReportRepository(self.storage)
        self.jobs = JobLeaseRepository(self.storage)
        self.usage = UsageLedger(self.storage)
        self.ai_usage = AiUsageRepository(self.storage)
        self.feedback = FeedbackRepository(self.storage)
        self.recommendations = self.feedback.recommendations
        self.recommendation = self.feedback.recommendation
        self.recommendation_views_for_report = self.feedback.recommendation_views_for_report
        self.recommendation_feedback = self.feedback.recommendation_feedback
        self.recommendation_status_counts = self.feedback.recommendation_status_counts
        self.stale_recommendation_count = self.feedback.stale_recommendation_count
        self.expire_stale_recommendations = self.feedback.expire_stale_recommendations
        self.set_recommendation_feedback = self.feedback.set_recommendation_feedback
        self.intervention_history = self.feedback.intervention_history
        self.mark_applied = self.feedback.mark_applied
        self.reject = self.feedback.reject
        self.flush_log_outbox = self.feedback.flush_log_outbox

    def initialize(self) -> None:
        self.storage.initialize()

    def close(self) -> None:
        self.storage.close()

    @property
    def identity(self) -> str:
        return hashlib.sha256((self.storage.config.endpoint + self.storage.path).encode()).hexdigest()

    def get_schema_revision(self) -> str:
        rows = self.storage.execute("SELECT value FROM metadata WHERE name='schema_version';")[0].rows
        return str(rows[0].value)

    def save_devices(self, devices: Any) -> int:
        from zont_analyzer.adapters.zont_readonly.equipment import equipment_facts
        from zont_analyzer.application.owner_context import OwnerContextStore

        captured_at = datetime.now(UTC).isoformat()
        wrapped = []
        for device in devices:
            identifier = str(device.get("device_id") or device.get("id") or "")
            if not identifier:
                continue
            wrapped.append({"id": identifier, "name": str(device.get("name") or device.get("alias") or identifier),
                            "model": device.get("devtype") or device.get("model"), "raw": device,
                            "discovered_at": captured_at})
        saved = self.telemetry.save_devices(wrapped)
        store = OwnerContextStore(self)
        for device in wrapped:
            raw = device["raw"]
            facts = raw.get("_equipment", equipment_facts(raw))
            if "_equipment" in raw and isinstance(facts, dict):
                facts = dict(facts)
                for field in ("coordinates", "boiler_model"):
                    facts.setdefault(field, {"value": None, "source": f"zont:devices.{field}:unavailable_or_ambiguous"})
            if isinstance(facts, dict) and facts:
                store.observe_auto(device["id"], facts)
        return saved

    def list_devices(self) -> list[dict[str, Any]]:
        result = []
        for device in self.telemetry.list_devices():
            if "raw" in device:
                result.append(device)
                continue
            device_id = str(device.get("device_id") or device.get("id"))
            result.append({"id": device_id, "name": str(device.get("name") or device.get("alias") or device_id),
                           "model": device.get("devtype") or device.get("model"), "raw": device})
        return result

    def list_series(self) -> list[dict[str, Any]]:
        return self.telemetry.list_series()

    def upsert_entity(self, **values: Any) -> None:
        self.telemetry.upsert_entity(**values)

    def update_series_role(
        self, series_id: int, role: str, display_name: str | None = None, *,
        confidence: float | None = None, provenance: str | None = None, origin: str | None = None,
    ) -> None:
        def write(tx: Transaction) -> None:
            rows = tx.execute("DECLARE $id AS Int64; SELECT * FROM telemetry_series VIEW by_id WHERE id=$id;",
                              {"$id": series_id})[0].rows
            if not rows:
                return
            row = rows[0]
            payload = json.loads(row.payload)
            payload["role"] = role
            if display_name:
                payload["display_name"] = display_name
            for key, value in {"confidence": confidence, "provenance": provenance, "origin": origin}.items():
                if value is not None:
                    payload[key] = value
            encoded = encode(payload)
            if encoded == row.payload:
                return
            tx.execute(
                "DECLARE $device AS Utf8; DECLARE $source AS Utf8; DECLARE $entity AS Utf8; "
                "DECLARE $metric AS Utf8; DECLARE $payload AS Utf8; UPDATE telemetry_series "
                "SET payload=$payload WHERE device_id=$device AND source_type=$source "
                "AND entity_id=$entity AND metric_key=$metric;",
                {"$device": row.device_id, "$source": row.source_type, "$entity": row.entity_id,
                 "$metric": row.metric_key, "$payload": encoded},
            )
            bump_revision(tx, "series:" + row.device_id)
        self.storage.transaction(write)

    def _samples(self, series_id: int, start: datetime, end: datetime) -> Iterator[dict[str, Any]]:
        after = None
        while True:
            page = self.telemetry.read_samples(series_id, start, end, after=after)
            yield from page
            if len(page) < 2000:
                return
            after = page[-1]["timestamp_utc"]

    def fetch_samples(self, series_id: int, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        return [(datetime.fromtimestamp(row["timestamp_utc"], UTC), float(row["value_num"]))
                for row in self._samples(series_id, start, end)
                if row["quality"] == "valid" and row["value_num"] is not None]

    def fetch_numeric_observations(
        self, series_id: int, start: datetime, end: datetime, *, include_previous: bool = False,
    ) -> list[tuple[datetime, float | None]]:
        rows = list(self._samples(series_id, start, end))
        if include_previous:
            previous = self.storage.execute(
                "DECLARE $id AS Int64; DECLARE $start AS Int64; SELECT * FROM telemetry_samples "
                "WHERE series_id=$id AND timestamp_utc < $start ORDER BY timestamp_utc DESC LIMIT 1;",
                {"$id": series_id, "$start": utc_seconds(start)},
            )[0].rows
            rows = [dict(row) for row in previous] + rows
        return [(datetime.fromtimestamp(row["timestamp_utc"], UTC),
                 float(row["value_num"]) if row["quality"] == "valid" and row["value_num"] is not None
                 and math.isfinite(row["value_num"]) else None) for row in rows]

    def fetch_text_samples(self, series_id: int, start: datetime, end: datetime) -> list[tuple[datetime, str]]:
        return [(datetime.fromtimestamp(row["timestamp_utc"], UTC), str(row["value_text"]))
                for row in self._samples(series_id, start, end)
                if row["quality"] == "valid" and row["value_text"] is not None]

    def fetch_sample_timestamps(self, series_id: int, start: datetime, end: datetime) -> list[datetime]:
        return [datetime.fromtimestamp(row["timestamp_utc"], UTC) for row in self._samples(series_id, start, end)
                if row["quality"] == "valid"]

    def fetch_device_sample_timestamps(self, device_id: str, start: datetime, end: datetime) -> list[datetime]:
        return sorted({timestamp for series in self.list_series() if series["device_id"] == device_id
                       for timestamp in self.fetch_sample_timestamps(series["id"], start, end)})

    def _sample_boundary(self, *, latest: bool) -> datetime | None:
        ordering = "DESC" if latest else "ASC"
        values = []
        for series in self.list_series():
            rows = self.storage.execute(
                "DECLARE $id AS Int64; SELECT timestamp_utc FROM telemetry_samples WHERE series_id=$id "
                f"ORDER BY timestamp_utc {ordering} LIMIT 1;", {"$id": series["id"]},
            )[0].rows
            if rows:
                values.append(int(rows[0].timestamp_utc))
        if not values:
            return None
        return datetime.fromtimestamp((max if latest else min)(values), UTC)

    def latest_sample_time(self) -> datetime | None:
        return self._sample_boundary(latest=True)

    def earliest_sample_time(self) -> datetime | None:
        return self._sample_boundary(latest=False)

    def get_cursor(self, device_id: str, data_type: str) -> datetime | None:
        return self.telemetry.get_cursor(device_id, data_type)

    def set_app_meta(self, key: str, value: str) -> None:
        self.storage.execute("DECLARE $key AS Utf8; DECLARE $value AS Utf8; "
                             "UPSERT INTO app_meta (key,value) VALUES ($key,$value);",
                             {"$key": key, "$value": value})

    def get_app_meta(self, key: str) -> str | None:
        rows = self.storage.execute("DECLARE $key AS Utf8; SELECT value FROM app_meta WHERE key=$key;",
                                    {"$key": key})[0].rows
        return str(rows[0].value) if rows else None

    def report(self, report_id: str) -> Report | None:
        return self.reports.report(report_id)

    def source_revision(self) -> int:
        rows = self.storage.execute("SELECT revision FROM revisions WHERE scope='publication';")[0].rows
        return int(rows[0].revision) if rows else 0

    def save_report(
        self, report: Report, rendered_text: str, *, source_revision: int | None = None,
        job_fence: tuple[str, str, int] | None = None,
    ) -> None:
        for index, recommendation in enumerate(report.recommendations, start=1):
            if recommendation.id is None:
                recommendation.id = f"rec:{report.id}:{index}"
        rows = self.storage.execute(
            "DECLARE $id AS Utf8; SELECT revision FROM reports VIEW by_id WHERE id=$id;",
            {"$id": report.id},
        )[0].rows
        self.reports.save_report(report, rendered_text, expected_revision=int(rows[0].revision) if rows else 0,
                                 source_revision=source_revision, job_fence=job_fence)

    def prior_reports(self, before: datetime, *, limit: int = 7) -> list[Report]:
        return self.reports.prior_reports(before, limit=limit)

    def completed_reports(self, now: datetime) -> list[Report]:
        return sorted((report for report in self._all_reports()
                       if report.kind in {"daily", "weekly", "monthly", "seasonal"}
                       and report.period_end <= now and report.generated_at >= report.period_end),
                      key=lambda report: (report.generated_at, report.id))

    def _all_reports(self) -> Iterator[Report]:
        after, after_id = -1, ""
        while True:
            rows = self.storage.execute(
                "DECLARE $after AS Int64; DECLARE $id AS Utf8; SELECT id,period_end,payload FROM reports "
                "WHERE period_end > $after OR (period_end=$after AND id > $id) "
                "ORDER BY period_end,id LIMIT 100;", {"$after": after, "$id": after_id},
            )[0].rows
            for row in rows:
                yield Report.model_validate(json.loads(row.payload)["report"])
            if len(rows) < 100:
                return
            after, after_id = int(rows[-1].period_end), str(rows[-1].id)

    def latest_report(self) -> Report | None:
        return max(self._all_reports(), key=lambda report: report.generated_at, default=None)

    def token_usage_this_month(self) -> int:
        return self.ai_usage.token_usage_this_month()

    def status(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for label, table in (("devices", "devices"), ("series", "telemetry_series"),
                             ("samples", "telemetry_samples"), ("source_events", "source_events"),
                             ("reports", "reports")):
            rows = self.storage.execute(f"SELECT COUNT(*) AS count FROM `{table}`;")[0].rows
            counts[label] = int(rows[0].count)
        pending = self.storage.execute(
            "SELECT COUNT(*) AS count FROM notification_outbox WHERE state='pending';",
        )[0].rows
        first, last = self.earliest_sample_time(), self.latest_sample_time()
        return {"storage": "YDB", "schema_revision": self.get_schema_revision(), **counts,
                "pending_notifications": int(pending[0].count),
                "earliest_sample": first.isoformat() if first else None,
                "latest_sample": last.isoformat() if last else None,
                "monthly_ai_tokens": self.token_usage_this_month()}

    def list_source_events(self, start: datetime, end: datetime) -> list[SourceEvent]:
        events: list[SourceEvent] = []
        for device in self.list_devices():
            after, after_id = utc_seconds(start) - 1, ""
            while True:
                rows = self.storage.execute(
                    "DECLARE $device AS Utf8; DECLARE $start AS Int64; DECLARE $end AS Int64; "
                    "DECLARE $after AS Int64; DECLARE $id AS Utf8; SELECT timestamp_utc,id,payload "
                    "FROM source_events WHERE device_id=$device AND timestamp_utc >= $start "
                    "AND timestamp_utc < $end AND (timestamp_utc > $after OR "
                    "(timestamp_utc=$after AND id > $id)) ORDER BY timestamp_utc,id LIMIT 2000;",
                    {"$device": device["id"], "$start": utc_seconds(start), "$end": utc_seconds(end),
                     "$after": after, "$id": after_id},
                )[0].rows
                events.extend(SourceEvent.model_validate_json(row.payload) for row in rows)
                if len(rows) < 2000:
                    break
                after, after_id = rows[-1].timestamp_utc, rows[-1].id
        return sorted(events, key=lambda event: (event.timestamp_utc, event.id))

    def period_data_revision(self, start: datetime, end: datetime) -> str:
        if end <= start:
            raise ValueError("period end must be after its start")
        source_revision = self.source_revision()
        markers = self.legacy_period_data_revision(start, end)
        key = (f"telemetry-period-revision:v2:{start.astimezone(UTC).isoformat(timespec='microseconds')}:"
               f"{end.astimezone(UTC).isoformat(timespec='microseconds')}")
        encoded = self.get_app_meta(key)
        if encoded:
            try:
                cached = json.loads(encoded)
                if isinstance(cached, dict) and cached.get("markers") == markers:
                    revision = cached.get("revision")
                    if isinstance(revision, str):
                        return revision
            except (ValueError, TypeError):
                pass
        digest = hashlib.sha256()
        count = 0
        for series in self.list_series():
            for row in self._samples(series["id"], start, end):
                digest.update(json.dumps([series["id"], row["timestamp_utc"], row["value_num"],
                                          row["value_text"], row["quality"]], ensure_ascii=False,
                                         allow_nan=False, separators=(",", ":")).encode())
                digest.update(b"\n")
                count += 1
        revision = f"telemetry-v2:{digest.hexdigest()}" if count else hashlib.sha256(b"[]").hexdigest()

        def save(tx: Transaction) -> None:
            current = tx.execute("SELECT revision FROM revisions WHERE scope='publication';")[0].rows
            if (int(current[0].revision) if current else 0) != source_revision:
                raise ValueError("inputs changed during telemetry fingerprint calculation")
            tx.execute(
                "DECLARE $key AS Utf8; DECLARE $value AS Utf8; "
                "UPSERT INTO app_meta (key,value) VALUES ($key,$value);",
                {"$key": key, "$value": encode({"markers": markers, "revision": revision})},
            )
        self.storage.transaction(save)
        return revision

    def report_for_period(self, start: datetime, end: datetime) -> Report | None:
        rows = self.storage.execute(
            "DECLARE $start AS Int64; DECLARE $end AS Int64; SELECT payload FROM reports "
            "WHERE period_start=$start AND period_end=$end;",
            {"$start": utc_seconds(start), "$end": utc_seconds(end)},
        )[0].rows
        reports = [Report.model_validate(json.loads(row.payload)["report"]) for row in rows]
        return max(reports, key=lambda report: report.generated_at) if reports else None

    def daily_report_catalogue(self, start: datetime, end: datetime) -> Iterator[tuple[str, datetime]]:
        """Read small selection metadata before loading bounded historical context."""
        for row in self._daily_report_rows(start, end, include_payload=False):
            yield str(row.id), datetime.fromtimestamp(row.period_start, UTC)

    def _daily_report_rows(self, start: datetime, end: datetime, *, include_payload: bool) -> Iterator[Any]:
        after, after_id = utc_seconds(start) - 1, ""
        columns = "id,period_start,payload" if include_payload else "id,period_start"
        while True:
            rows = self.storage.execute(
                "DECLARE $start AS Int64; DECLARE $end AS Int64; DECLARE $after AS Int64; "
                f"DECLARE $id AS Utf8; SELECT {columns} FROM reports WHERE kind='daily' "
                "AND period_start >= $start AND period_end <= $end AND (period_start > $after OR "
                "(period_start=$after AND id > $id)) ORDER BY period_start,id LIMIT 100;",
                {"$start": utc_seconds(start), "$end": utc_seconds(end), "$after": after, "$id": after_id},
            )[0].rows
            yield from rows
            if len(rows) < 100:
                return
            after, after_id = rows[-1].period_start, rows[-1].id

    def daily_reports(self, start: datetime, end: datetime) -> Iterator[Report]:
        """Stream complete days in order, choosing the latest generated version."""
        pending: list[Report] = []
        for row in self._daily_report_rows(start, end, include_payload=True):
            report = Report.model_validate(json.loads(row.payload)["report"])
            if pending and pending[0].period_start != report.period_start:
                yield max(pending, key=lambda item: item.generated_at)
                pending.clear()
            pending.append(report)
        if pending:
            yield max(pending, key=lambda item: item.generated_at)

    def source_event_revision(self, end: datetime) -> str:
        digest = hashlib.sha256()
        for event in self.list_source_events(datetime(1970, 1, 1, tzinfo=UTC), end):
            values = [event.id, event.device_id, event.event_type, utc_seconds(event.timestamp_utc),
                      event.duration_seconds, json.dumps(event.details, ensure_ascii=False, sort_keys=True),
                      event.important]
            digest.update(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode())
            digest.update(b"\n")
        return f"source-events-v1:{digest.hexdigest()}"

    def legacy_period_data_revision(self, start: datetime, end: datetime) -> str:
        """Compare imported day markers before adopting a content fingerprint."""
        if end <= start:
            raise ValueError("period end must be after its start")
        rows = self.storage.execute(
            "DECLARE $first AS Utf8; DECLARE $last AS Utf8; "
            "SELECT key,value FROM app_meta WHERE key >= $first AND key <= $last ORDER BY key;",
            {"$first": f"telemetry-day:{start.astimezone(UTC).date().isoformat()}",
             "$last": f"telemetry-day:{(end - timedelta(microseconds=1)).astimezone(UTC).date().isoformat()}"},
        )[0].rows
        return hashlib.sha256(json.dumps([[row.key, row.value] for row in rows]).encode()).hexdigest()

    def upgrade_report_telemetry_revision(self, report_id: str, old_revision: str, new_revision: str) -> bool:
        return self._upgrade_report_marker(report_id, ("input_revision", "telemetry"), old_revision, new_revision)

    def upgrade_report_schedule_signature(self, report_id: str, old_signature: str, new_signature: str) -> bool:
        return self._upgrade_report_marker(report_id, ("schedule_signature",), old_signature, new_signature)

    def _upgrade_report_marker(self, report_id: str, path: tuple[str, ...], old_value: str, new_value: str) -> bool:
        original = self.report(report_id)
        if original is None:
            return False
        refreshed = original.model_copy(deep=True)
        context = refreshed.context
        for key in path[:-1]:
            child = context.get(key)
            if not isinstance(child, dict):
                return False
            context = child
        if context.get(path[-1]) != old_value:
            return False
        context[path[-1]] = new_value
        return self.reports.replace_context(original, refreshed,
                                            f"report-marker-upgrade:{report_id}:{':'.join(path)}", old_value)

    def seed_source_event_report_baselines(self) -> int:
        completion = "source-event-report-baselines:v1:complete"
        if self.get_app_meta(completion) == "1":
            return 0
        source_revision = self.source_revision()
        after, after_id, count = -1, "", 0
        revisions: dict[int, str] = {}
        while True:
            rows = self.storage.execute(
                "DECLARE $after AS Int64; DECLARE $id AS Utf8; SELECT id,period_end FROM reports "
                "WHERE period_end > $after OR (period_end=$after AND id > $id) "
                "ORDER BY period_end,id LIMIT 100;", {"$after": after, "$id": after_id},
            )[0].rows
            for row in rows:
                key = f"source-event-report-baseline:v1:{row.id}"
                if self.get_app_meta(key) is not None:
                    continue
                end = int(row.period_end)
                if end not in revisions:
                    revisions[end] = self.source_event_revision(datetime.fromtimestamp(end, UTC))
                value = revisions[end]

                def write(tx: Transaction, key: str = key, value: str = value) -> None:
                    current = tx.execute("SELECT revision FROM revisions WHERE scope='publication';")[0].rows
                    if (int(current[0].revision) if current else 0) != source_revision:
                        raise ValueError("inputs changed during baseline initialization")
                    existing = tx.execute(
                        "DECLARE $key AS Utf8; SELECT key FROM app_meta WHERE key=$key;", {"$key": key},
                    )[0].rows
                    if not existing:
                        tx.execute(
                            "DECLARE $key AS Utf8; DECLARE $value AS Utf8; "
                            "UPSERT INTO app_meta (key,value) VALUES ($key,$value);",
                            {"$key": key, "$value": value},
                        )
                self.storage.transaction(write)
                count += 1
            if len(rows) < 100:
                break
            after, after_id = int(rows[-1].period_end), str(rows[-1].id)
        self.set_app_meta(completion, "1")
        return count
