"""Atomic report, recommendation feedback, and publication storage in YDB."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar

import ydb  # type: ignore[import-untyped]

from zont_analyzer.domain.models import Report

T = TypeVar("T")


class Transaction(Protocol):
    def execute(
        self, query: str, parameters: dict[str, Any] | None = None
    ) -> list[Any]: ...


class Database(Protocol):
    def transaction(self, callback: Callable[[Transaction], T]) -> T: ...


def _first(result_sets: list[Any]) -> Any | None:
    return result_sets[0].rows[0] if result_sets and result_sets[0].rows else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _seconds(value: datetime, *, exact: bool = False) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("report times must include a timezone")
    if exact and value.microsecond:
        raise ValueError("report period boundaries must have whole-second precision")
    return math.floor(value.astimezone(UTC).timestamp())


def _clock_us() -> int:
    return time.time_ns() // 1_000


@dataclass(frozen=True)
class Feedback:
    recommendation_id: str
    report_id: str
    status: str
    note: str | None
    experiment: dict[str, Any] | None
    updated_at: int
    recommendation: dict[str, Any]


@dataclass(frozen=True)
class FeedbackAudit:
    id: int
    recommendation_id: str
    at: int
    payload: dict[str, Any]


def _feedback(row: Any) -> Feedback:
    experiment = _text(row["experiment"])
    return Feedback(
        recommendation_id=_text(row["id"]) or "",
        report_id=_text(row["report_id"]) or "",
        status=_text(row["status"]) or "",
        note=_text(row["note"]),
        experiment=json.loads(experiment) if experiment is not None else None,
        updated_at=int(row["updated_at"]),
        recommendation=json.loads(_text(row["payload"]) or "{}"),
    )


_GET_REPORT = """
DECLARE $kind AS Utf8;
DECLARE $start AS Int64;
DECLARE $end AS Int64;
DECLARE $version AS Utf8;
SELECT id, payload, revision FROM reports
WHERE kind = $kind AND period_start = $start AND period_end = $end
  AND algorithm_version = $version;
"""

_REPORT_BY_ID = """
DECLARE $id AS Utf8;
SELECT kind, period_start, period_end, algorithm_version, id, payload, revision
FROM reports VIEW by_id WHERE id = $id LIMIT 2;
"""

_PUT_REPORT = """
DECLARE $kind AS Utf8;
DECLARE $start AS Int64;
DECLARE $end AS Int64;
DECLARE $version AS Utf8;
DECLARE $id AS Utf8;
DECLARE $payload AS Utf8;
DECLARE $revision AS Int64;
UPSERT INTO reports (kind, period_start, period_end, algorithm_version, id, payload, revision)
VALUES ($kind, $start, $end, $version, $id, $payload, $revision);
"""

_DELETE_REPORT = """
DECLARE $kind AS Utf8;
DECLARE $start AS Int64;
DECLARE $end AS Int64;
DECLARE $version AS Utf8;
DELETE FROM reports WHERE kind = $kind AND period_start = $start
  AND period_end = $end AND algorithm_version = $version;
"""

_GET_REVISION = """
DECLARE $scope AS Utf8;
SELECT revision FROM revisions WHERE scope = $scope;
"""

_PUT_REVISION = """
DECLARE $scope AS Utf8;
DECLARE $revision AS Int64;
UPSERT INTO revisions (scope, revision) VALUES ($scope, $revision);
"""

_GET_RECOMMENDATION = """
DECLARE $id AS Utf8;
SELECT id, report_id, payload, status, note, experiment, updated_at
FROM recommendations WHERE id = $id;
"""

_PUT_RECOMMENDATION = """
DECLARE $id AS Utf8;
DECLARE $report_id AS Utf8;
DECLARE $payload AS Utf8;
DECLARE $status AS Utf8;
DECLARE $note AS Utf8?;
DECLARE $experiment AS Utf8?;
DECLARE $updated_at AS Int64;
UPSERT INTO recommendations (id, report_id, payload, status, note, experiment, updated_at)
VALUES ($id, $report_id, $payload, $status, $note, $experiment, $updated_at);
"""

_GET_OUTBOX = """
DECLARE $id AS Utf8;
SELECT id FROM notification_outbox WHERE id = $id;
"""

_PUT_OUTBOX = """
DECLARE $id AS Utf8;
DECLARE $report_id AS Utf8;
DECLARE $payload AS Utf8;
UPSERT INTO notification_outbox (id, report_id, channel, payload, state, attempts)
VALUES ($id, $report_id, 'log', $payload, 'pending', 0);
"""

_PUT_PUBLICATION = """
DECLARE $identifier AS Utf8;
DECLARE $revision AS Int64;
DECLARE $payload AS Utf8;
UPSERT INTO publication_changes (scope, identifier, revision, payload)
VALUES ('report', $identifier, $revision, $payload);
"""

_PUT_AUDIT = """
DECLARE $id AS Int64;
DECLARE $recommendation_id AS Utf8;
DECLARE $at AS Int64;
DECLARE $payload AS Utf8;
UPSERT INTO recommendation_audit (id, recommendation_id, at, payload)
VALUES ($id, $recommendation_id, $at, $payload);
"""


class ReportRepository:
    """A report and its derived durable state commit in one SerializableRW transaction."""

    def __init__(self, db: Database, *, clock: Callable[[], int] = _clock_us) -> None:
        self.db = db
        self.clock = clock

    def save_report(
        self,
        report: Report,
        rendered_text: str,
        *,
        expected_revision: int = 0,
        telemetry_scope: str | None = None,
        telemetry_revision: int | None = None,
        source_revision: int | None = None,
        job_fence: tuple[str, str, int] | None = None,
    ) -> int:
        if expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        if (telemetry_scope is None) != (telemetry_revision is None):
            raise ValueError("telemetry scope and revision must be supplied together")
        if _seconds(report.period_end, exact=True) <= _seconds(report.period_start, exact=True):
            raise ValueError("report period must have positive duration")
        normalized = report.model_copy(deep=True)
        for index, recommendation in enumerate(normalized.recommendations):
            if recommendation.id is None:
                recommendation.id = f"rec:{normalized.id}:{index + 1}"
        ids = [item.id for item in normalized.recommendations]
        if len(ids) != len(set(ids)):
            raise ValueError("recommendation IDs must be unique")
        key = {
            "$kind": normalized.kind,
            "$start": _seconds(normalized.period_start, exact=True),
            "$end": _seconds(normalized.period_end, exact=True),
            "$version": normalized.algorithm_version,
        }
        payload = _json({"report": normalized.model_dump(mode="json"), "rendered_text": rendered_text})
        now = self.clock()

        def save(tx: Transaction) -> int:
            job_checkpoint: dict[str, Any] | None = None
            if job_fence is not None:
                job_key, owner, attempt = job_fence
                job = _first(tx.execute(
                    "DECLARE $key AS Utf8; SELECT owner,attempt,state,lease_until,checkpoint "
                    "FROM jobs WHERE job_key=$key;",
                    {"$key": job_key},
                ))
                if (job is None or _text(job["owner"]) != owner or int(job["attempt"]) != attempt
                        or _text(job["state"]) != "active" or int(job["lease_until"]) <= self.clock()):
                    raise ValueError("job ownership expired before report commit")
                checkpoint_text = _text(job["checkpoint"])
                job_checkpoint = json.loads(checkpoint_text) if checkpoint_text else {}
                if (job_checkpoint.get("phase") != "analyze"
                        or not isinstance(job_checkpoint.get("input_fingerprint"), str)
                        or job_checkpoint.get("source_revision") != source_revision):
                    raise ValueError("job input snapshot changed before report commit")
            row = _first(tx.execute(_GET_REPORT, key))
            by_id = tx.execute(_REPORT_BY_ID, {"$id": normalized.id})[0].rows
            if len(by_id) > 1:
                raise ValueError("report ID belongs to multiple periods")
            previous = by_id[0] if by_id else None
            previous_key: tuple[str, int, int, str] | None = (
                _text(previous["kind"]) or "", int(previous["period_start"]),
                int(previous["period_end"]), _text(previous["algorithm_version"]) or "",
            ) if previous is not None else None
            current_key = (normalized.kind, key["$start"], key["$end"], normalized.algorithm_version)
            moving_season = previous_key is not None and previous_key != current_key
            if moving_season:
                assert previous_key is not None
                if not (
                    normalized.kind == "seasonal" and previous_key[0] == "seasonal"
                    and previous_key[1] == _seconds(normalized.period_start, exact=True)
                    and previous_key[3] == normalized.algorithm_version
                    and previous_key[2] < _seconds(normalized.period_end, exact=True)
                ):
                    raise ValueError("report ID already belongs to a different period")
            old_revision = int(previous["revision"]) if previous is not None else 0
            if source_revision is not None:
                current = _first(tx.execute(_GET_REVISION, {"$scope": "publication"}))
                if (int(current["revision"]) if current is not None else 0) != source_revision:
                    raise ValueError("inputs changed while report was calculated")
            if telemetry_scope is not None:
                current = _first(tx.execute(_GET_REVISION, {"$scope": telemetry_scope}))
                observed = int(current["revision"]) if current is not None else 0
                if observed != telemetry_revision:
                    raise ValueError("telemetry changed while report was calculated")
            if row is not None:
                if _text(row["id"]) != normalized.id:
                    raise ValueError("report period already belongs to a different ID")
                if _text(row["payload"]) == payload:
                    if job_checkpoint is not None:
                        self._save_job_checkpoint(tx, job_key, job_checkpoint, normalized)
                    return old_revision
            if old_revision != expected_revision:
                raise ValueError("stale report revision")
            revision = old_revision + 1
            if moving_season:
                assert previous_key is not None
                tx.execute(_DELETE_REPORT, {
                    "$kind": previous_key[0], "$start": previous_key[1],
                    "$end": previous_key[2], "$version": previous_key[3],
                })
            tx.execute(_PUT_REPORT, {**key, "$id": normalized.id,
                                     "$payload": payload, "$revision": revision})
            for recommendation in normalized.recommendations:
                rec_id = recommendation.id
                assert rec_id is not None
                existing = _first(tx.execute(_GET_RECOMMENDATION, {"$id": rec_id}))
                if existing is not None and _text(existing["report_id"]) != normalized.id:
                    raise ValueError("recommendation ID belongs to another report")
                self._put_recommendation(
                    tx, rec_id, normalized.id, recommendation.model_dump_json(),
                    _text(existing["status"]) or "new" if existing is not None else "new",
                    _text(existing["note"]) if existing is not None else None,
                    _text(existing["experiment"]) if existing is not None else None,
                    int(existing["updated_at"]) if existing is not None else now,
                )
                if existing is None:
                    tx.execute(
                        "DECLARE $id AS Utf8; DECLARE $created AS Int64; "
                        "UPDATE recommendations SET created_at=$created WHERE id=$id;",
                        {"$id": rec_id, "$created": now},
                    )
            outbox_id = f"outbox:{normalized.id}:log"
            if _first(tx.execute(_GET_OUTBOX, {"$id": outbox_id})) is None:
                tx.execute(_PUT_OUTBOX, {"$id": outbox_id,
                                         "$report_id": normalized.id, "$payload": rendered_text})
            publication_revision = self._next_revision(tx, "publication")
            tx.execute(_PUT_PUBLICATION, {
                "$identifier": normalized.id,
                "$revision": publication_revision,
                "$payload": _json({"report_id": normalized.id, "report_revision": revision}),
            })
            if job_checkpoint is not None:
                self._save_job_checkpoint(tx, job_key, job_checkpoint, normalized)
            return revision

        return self.db.transaction(save)

    @staticmethod
    def _save_job_checkpoint(
        tx: Transaction, job_key: str, checkpoint: dict[str, Any], report: Report,
    ) -> None:
        saved = {
            "phase": "saved", "report_id": report.id, "ai_used": report.ai_used,
            "input_fingerprint": checkpoint["input_fingerprint"],
        }
        tx.execute(
            "DECLARE $key AS Utf8; DECLARE $checkpoint AS Utf8; "
            "UPDATE jobs SET checkpoint=$checkpoint WHERE job_key=$key;",
            {"$key": job_key, "$checkpoint": _json(saved)},
        )

    def report(self, report_id: str) -> Report | None:
        def read(tx: Transaction) -> Report | None:
            rows = tx.execute(_REPORT_BY_ID, {"$id": report_id})[0].rows
            if len(rows) > 1:
                raise ValueError("duplicate report ID")
            return self._load(rows[0]) if rows else None

        return self.db.transaction(read)

    def replace_context(self, original: Report, refreshed: Report, history_key: str, history_value: str) -> bool:
        """Compare-and-set derived context without changing feedback or delivery state."""
        if original.model_copy(update={"context": refreshed.context}) != refreshed:
            raise ValueError("context refresh may only change report context")

        def write(tx: Transaction) -> bool:
            rows = tx.execute(_REPORT_BY_ID, {"$id": original.id})[0].rows
            if not rows:
                return True
            row = rows[0]
            payload = json.loads(_text(row["payload"]) or "{}")
            if Report.model_validate(payload["report"]) != original:
                return False
            if original.context == refreshed.context:
                return True
            tx.execute(
                "DECLARE $key AS Utf8; DECLARE $value AS Utf8; "
                "UPSERT INTO app_meta (key,value) VALUES ($key,$value);",
                {"$key": history_key, "$value": history_value},
            )
            payload["report"] = refreshed.model_dump(mode="json")
            tx.execute(_PUT_REPORT, {
                "$kind": row["kind"], "$start": row["period_start"], "$end": row["period_end"],
                "$version": row["algorithm_version"], "$id": original.id,
                "$payload": _json(payload), "$revision": int(row["revision"]) + 1,
            })
            return True

        return self.db.transaction(write)

    def prior_reports(self, before: datetime, *, limit: int = 7) -> list[Report]:
        bound = max(0, min(limit, 7))
        if bound == 0:
            return []
        query = """
        DECLARE $before AS Int64;
        DECLARE $limit AS Uint64;
        SELECT payload FROM reports
        WHERE kind = 'daily' AND period_end <= $before
        ORDER BY period_end DESC, id LIMIT $limit;
        """
        return self._list(query, {"$before": _seconds(before),
                                  "$limit": ydb.TypedValue(bound, ydb.PrimitiveType.Uint64)})

    def completed_reports(self, now: datetime, *, limit: int = 1000) -> list[Report]:
        bound = max(0, min(limit, 1000))
        if bound == 0:
            return []
        query = """
        DECLARE $now AS Int64;
        DECLARE $limit AS Uint64;
        SELECT payload FROM reports
        WHERE kind IN ('daily', 'weekly', 'monthly', 'seasonal') AND period_end <= $now
        ORDER BY period_end ASC, kind, id LIMIT $limit;
        """
        reports = self._list(query, {"$now": _seconds(now),
                                     "$limit": ydb.TypedValue(bound, ydb.PrimitiveType.Uint64)})
        return sorted(
            (report for report in reports if report.generated_at >= report.period_end),
            key=lambda report: (report.generated_at, report.id),
        )

    def feedback(self, recommendation_id: str) -> Feedback | None:
        def read(tx: Transaction) -> Feedback | None:
            row = _first(tx.execute(_GET_RECOMMENDATION, {"$id": recommendation_id}))
            return _feedback(row) if row is not None else None

        return self.db.transaction(read)

    def set_feedback(
        self,
        recommendation_id: str,
        status: str,
        note: str | None = None,
        experiment: dict[str, Any] | None = None,
        *,
        expected_updated_at: int | None = None,
    ) -> Feedback:
        if status not in ("applied", "rejected"):
            raise ValueError("status must be applied or rejected")
        if status == "rejected" and experiment is not None:
            raise ValueError("experiment requires applied feedback")
        normalized_note = (note or "").strip() or None
        experiment_json = _json(experiment) if experiment is not None else None
        now = self.clock()

        def save(tx: Transaction) -> Feedback:
            row = _first(tx.execute(_GET_RECOMMENDATION, {"$id": recommendation_id}))
            if row is None:
                raise KeyError(recommendation_id)
            old = _feedback(row)
            if (old.status == status and old.note == normalized_note
                    and old.experiment == experiment):
                return old
            if expected_updated_at is not None and old.updated_at != expected_updated_at:
                raise ValueError("stale recommendation feedback")
            # Increasing even under a frozen test clock supplies a useful CAS token.
            updated_at = max(now, old.updated_at + 1)
            self._put_recommendation(
                tx, recommendation_id, old.report_id, _text(row["payload"]) or "{}",
                status, normalized_note, experiment_json, updated_at,
            )
            audit_id = self._next_revision(tx, "recommendation_audit")
            tx.execute(_PUT_AUDIT, {
                "$id": audit_id, "$recommendation_id": recommendation_id,
                "$at": updated_at,
                "$payload": _json({"status": status, "note": normalized_note,
                                   "experiment": experiment}),
            })
            return Feedback(
                recommendation_id, old.report_id, status, normalized_note,
                experiment, updated_at, old.recommendation,
            )

        return self.db.transaction(save)

    def feedback_history(self, *, limit: int = 100) -> list[Feedback]:
        bound = max(0, min(limit, 1000))
        if bound == 0:
            return []
        query = """
        DECLARE $limit AS Uint64;
        SELECT id, report_id, payload, status, note, experiment, updated_at
        FROM recommendations WHERE status IN ('applied', 'rejected')
        ORDER BY updated_at DESC, id LIMIT $limit;
        """

        def read(tx: Transaction) -> list[Feedback]:
            return [_feedback(row) for row in tx.execute(
                query, {"$limit": ydb.TypedValue(bound, ydb.PrimitiveType.Uint64)}
            )[0].rows]

        return self.db.transaction(read)

    def feedback_audit(
        self, recommendation_id: str | None = None, *, after_id: int = 0,
        limit: int = 100,
    ) -> list[FeedbackAudit]:
        """Read immutable owner decisions in keyset order, with a hard page bound."""
        if after_id < 0:
            raise ValueError("after_id must be non-negative")
        bound = max(0, min(limit, 1000))
        if bound == 0:
            return []
        parameters: dict[str, Any] = {
            "$after_id": after_id,
            "$limit": ydb.TypedValue(bound, ydb.PrimitiveType.Uint64),
        }
        if recommendation_id is None:
            query = """
            DECLARE $after_id AS Int64;
            DECLARE $limit AS Uint64;
            SELECT id, recommendation_id, at, payload FROM recommendation_audit
            WHERE id > $after_id ORDER BY id LIMIT $limit;
            """
        else:
            query = """
            DECLARE $after_id AS Int64;
            DECLARE $limit AS Uint64;
            DECLARE $recommendation_id AS Utf8;
            SELECT id, recommendation_id, at, payload FROM recommendation_audit
            WHERE id > $after_id AND recommendation_id = $recommendation_id
            ORDER BY id LIMIT $limit;
            """
            parameters["$recommendation_id"] = recommendation_id

        def read(tx: Transaction) -> list[FeedbackAudit]:
            return [FeedbackAudit(
                id=int(row["id"]),
                recommendation_id=_text(row["recommendation_id"]) or "",
                at=int(row["at"]),
                payload=json.loads(_text(row["payload"]) or "{}"),
            ) for row in tx.execute(query, parameters)[0].rows]

        return self.db.transaction(read)

    def _list(self, query: str, parameters: dict[str, Any]) -> list[Report]:
        def read(tx: Transaction) -> list[Report]:
            return [self._load(row) for row in tx.execute(query, parameters)[0].rows]

        return self.db.transaction(read)

    @staticmethod
    def _load(row: Any) -> Report:
        payload = json.loads(_text(row["payload"]) or "{}")
        return Report.model_validate(payload["report"])

    @staticmethod
    def _next_revision(tx: Transaction, scope: str) -> int:
        row = _first(tx.execute(_GET_REVISION, {"$scope": scope}))
        next_value = (int(row["revision"]) if row is not None else 0) + 1
        tx.execute(_PUT_REVISION, {"$scope": scope, "$revision": next_value})
        return next_value

    @staticmethod
    def _put_recommendation(
        tx: Transaction, rec_id: str, report_id: str, payload: str,
        status: str, note: str | None, experiment: str | None, updated_at: int,
    ) -> None:
        tx.execute(_PUT_RECOMMENDATION, {
            "$id": rec_id,
            "$report_id": report_id,
            "$payload": payload,
            "$status": status,
            "$note": ydb.TypedValue(note, ydb.OptionalType(ydb.PrimitiveType.Utf8)),
            "$experiment": ydb.TypedValue(experiment, ydb.OptionalType(ydb.PrimitiveType.Utf8)),
            "$updated_at": updated_at,
        })
