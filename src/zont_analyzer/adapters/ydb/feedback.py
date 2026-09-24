"""Owner feedback and intervention history stored in native YDB transactions."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, TypeVar

import ydb  # type: ignore[import-untyped]

from zont_analyzer.domain.experiments import Experiment, control_snapshot, snapshot_fingerprint

T = TypeVar("T")


class Transaction(Protocol):
    def execute(self, query: str, parameters: dict[str, Any] | None = None) -> list[Any]: ...


class Database(Protocol):
    def transaction(self, callback: Callable[[Transaction], T]) -> T: ...


def _first(results: list[Any]) -> Any | None:
    return results[0].rows[0] if results and results[0].rows else None


def _text(value: Any) -> str | None:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value) if value is not None else None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now_us() -> int:
    return time.time_ns() // 1_000


def _iso(value: int | str | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, int):
        return datetime.fromtimestamp(value / 1_000_000, UTC).isoformat()
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    return (moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)).isoformat()


def _micros(value: datetime) -> int:
    moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return int(moment.timestamp()) * 1_000_000 + moment.microsecond


_GET_REC = """
DECLARE $id AS Utf8;
SELECT id,report_id,payload,status,note,experiment,created_at,updated_at
FROM recommendations WHERE id=$id;
"""
_PUT_REC = """
DECLARE $id AS Utf8; DECLARE $report AS Utf8; DECLARE $payload AS Utf8;
DECLARE $status AS Utf8; DECLARE $note AS Utf8?; DECLARE $experiment AS Utf8?;
DECLARE $created AS Int64; DECLARE $updated AS Int64;
UPSERT INTO recommendations (id,report_id,payload,status,note,experiment,created_at,updated_at)
VALUES ($id,$report,$payload,$status,$note,$experiment,$created,$updated);
"""
_GET_INTERVENTIONS = """
DECLARE $id AS Utf8;
SELECT id,payload FROM interventions WHERE recommendation_id=$id
ORDER BY applied_at DESC,id DESC LIMIT 1;
"""
_GET_EXPERIMENT = """
DECLARE $id AS Utf8;
SELECT id,payload FROM intervention_experiments WHERE intervention_id=$id;
"""


class FeedbackRepository:
    def __init__(self, db: Database, *, clock: Callable[[], int] = _now_us) -> None:
        self.db, self.clock = db, clock

    @staticmethod
    def _latest_intervention(tx: Transaction, recommendation_id: str) -> dict[str, Any] | None:
        row = _first(tx.execute(_GET_INTERVENTIONS, {"$id": recommendation_id}))
        return dict(json.loads(_text(row.payload) or "{}")) if row is not None else None

    @staticmethod
    def _experiment(tx: Transaction, intervention_id: str | None) -> dict[str, Any] | None:
        if intervention_id is None:
            return None
        row = _first(tx.execute(_GET_EXPERIMENT, {"$id": intervention_id}))
        if row is None:
            return None
        payload = json.loads(_text(row.payload) or "{}")
        if "before_json" in payload or "after_json" in payload:
            value = {"category": payload.get("category"), "parameter": payload.get("parameter"),
                     "before": json.loads(payload["before_json"]) if payload.get("before_json") else None,
                     "after": json.loads(payload["after_json"]) if payload.get("after_json") else None,
                     "performed_at": _iso(payload.get("performed_at"))}
            snapshot = ({
                "value": json.loads(payload["snapshot_json"]),
                "fingerprint": payload.get("snapshot_fingerprint"),
                "captured_at": _iso(payload.get("snapshot_captured_at")),
                "source": payload.get("snapshot_source"),
                "historical_context": payload.get("historical_context"),
            } if payload.get("snapshot_json") is not None else None)
            return {**value, "control_snapshot": snapshot}
        value = dict(payload)
        if value.get("performed_at") is not None:
            value["performed_at"] = _iso(value["performed_at"])
        return value

    @classmethod
    def _view(cls, tx: Transaction, row: Any) -> dict[str, Any]:
        status = _text(row.status) or "new"
        intervention = cls._latest_intervention(tx, _text(row.id) or "") if status == "applied" else None
        return {
            **json.loads(_text(row.payload) or "{}"), "id": _text(row.id),
            "status": status, "report_id": _text(row.report_id),
            "owner_note": intervention.get("note") if intervention else _text(row.note),
            "updated_at": _iso(int(row.updated_at)),
            "intervention_id": intervention.get("id") if intervention else None,
            "experiment": cls._experiment(tx, intervention["id"] if intervention else None),
        }

    def recommendations(self) -> list[dict[str, Any]]:
        def read(tx: Transaction) -> list[dict[str, Any]]:
            rows = tx.execute("SELECT * FROM recommendations ORDER BY created_at DESC,id;")[0].rows
            return [self._view(tx, row) for row in rows]
        return self.db.transaction(read)

    def recommendation(self, recommendation_id: str) -> dict[str, Any] | None:
        return self.db.transaction(lambda tx: self._view(tx, row) if (
            row := _first(tx.execute(_GET_REC, {"$id": recommendation_id}))) is not None else None)

    def recommendation_views_for_report(self, report_id: str) -> dict[str, dict[str, Any]]:
        def read(tx: Transaction) -> dict[str, dict[str, Any]]:
            rows = tx.execute("DECLARE $report AS Utf8; SELECT * FROM recommendations "
                              "WHERE report_id=$report ORDER BY created_at,id;", {"$report": report_id})[0].rows
            return {str(row.id): self._view(tx, row) for row in rows}
        return self.db.transaction(read)

    def recommendation_feedback(self, limit: int = 10, *, before: datetime | None = None) -> list[dict[str, Any]]:
        if limit < 1:
            return []

        def read(tx: Transaction) -> list[dict[str, Any]]:
            rows = tx.execute(
                "SELECT * FROM recommendations WHERE status IN ('applied','rejected') "
                "ORDER BY updated_at DESC,id LIMIT 1000;",
            )[0].rows
            result: list[dict[str, Any]] = []
            for row in rows:
                view = self._view(tx, row)
                experiment = view["experiment"]
                boundary = experiment.get("performed_at") if experiment else None
                boundary = boundary or view["updated_at"]
                if before is not None and datetime.fromisoformat(boundary) >= before.astimezone(UTC):
                    continue
                payload = json.loads(_text(row.payload) or "{}")
                item = {
                    "recommendation_id": view["id"], "report_id": view["report_id"],
                    "status": view["status"], "title": payload.get("title"),
                    "category": payload.get("category"), "hypothesis": payload.get("hypothesis"),
                    "owner_note": view["owner_note"], "updated_at": view["updated_at"],
                }
                if experiment is not None:
                    item["experiment"] = experiment
                result.append(item)
                if len(result) >= limit:
                    break
            return result
        return self.db.transaction(read)

    def recommendation_status_counts(self) -> dict[str, int]:
        def read(tx: Transaction) -> dict[str, int]:
            counts = {status: 0 for status in ("new", "applied", "rejected", "ignored")}
            rows = tx.execute("SELECT status,COUNT(*) AS count FROM recommendations GROUP BY status;")[0].rows
            counts.update({str(row.status): int(row.count) for row in rows})
            return counts
        return self.db.transaction(read)

    def stale_recommendation_count(self, *, now: datetime | None = None) -> int:
        cutoff = _micros((now or datetime.now(UTC)) - timedelta(hours=48))
        def read(tx: Transaction) -> int:
            row = _first(tx.execute("DECLARE $cutoff AS Int64; SELECT COUNT(*) AS count FROM recommendations "
                                    "WHERE status='new' AND created_at <= $cutoff;", {"$cutoff": cutoff}))
            return int(row.count) if row else 0
        return self.db.transaction(read)

    def expire_stale_recommendations(self, *, now: datetime | None = None) -> dict[str, Any]:
        reference = now or datetime.now(UTC)
        cutoff = reference - timedelta(hours=48)
        cutoff_us, now_us = _micros(cutoff), _micros(reference)
        expired = 0
        while True:
            def expire(tx: Transaction) -> int:
                rows = tx.execute("DECLARE $cutoff AS Int64; SELECT * FROM recommendations "
                                  "WHERE status='new' AND created_at <= $cutoff ORDER BY id LIMIT 100;",
                                  {"$cutoff": cutoff_us})[0].rows
                for row in rows:
                    self._put_rec(tx, row, "ignored", _text(row.note), max(now_us, int(row.updated_at) + 1))
                    self._queue_render(tx, str(row.report_id))
                return len(rows)
            changed = self.db.transaction(expire)
            expired += changed
            if changed < 100:
                break
        return {"cutoff": cutoff.isoformat(), "eligible": expired, "ignored": expired,
                "status_counts": self.recommendation_status_counts()}

    @staticmethod
    def _put_rec(tx: Transaction, row: Any, status: str, note: str | None, updated_at: int) -> None:
        tx.execute(_PUT_REC, {
            "$id": row.id, "$report": row.report_id, "$payload": row.payload, "$status": status,
            "$note": ydb.TypedValue(note, ydb.OptionalType(ydb.PrimitiveType.Utf8)),
            "$experiment": ydb.TypedValue(_text(row.experiment), ydb.OptionalType(ydb.PrimitiveType.Utf8)),
            "$created": int(row.created_at), "$updated": updated_at,
        })

    @staticmethod
    def _report_payload(tx: Transaction, report_id: str) -> dict[str, Any] | None:
        rows = tx.execute("DECLARE $id AS Utf8; SELECT payload FROM reports VIEW by_id WHERE id=$id LIMIT 2;",
                          {"$id": report_id})[0].rows
        if len(rows) > 1:
            raise ValueError("duplicate report ID")
        return json.loads(_text(rows[0].payload) or "{}").get("report") if rows else None

    @classmethod
    def _control_snapshot(cls, tx: Transaction, report_id: str, performed_at: datetime | None) -> dict[str, Any] | None:
        report = cls._report_payload(tx, report_id)
        if report is None:
            return None
        context = report.get("context")
        device_id = context.get("device_id") if isinstance(context, dict) else None
        device = _first(tx.execute("DECLARE $id AS Utf8; SELECT payload FROM devices WHERE id=$id;",
                                   {"$id": str(device_id)})) if device_id else None
        if device is None:
            devices = tx.execute("SELECT payload FROM devices LIMIT 2;")[0].rows
            device = devices[0] if len(devices) == 1 else None
        if device is None:
            return None
        payload = json.loads(_text(device.payload) or "{}")
        snapshot = control_snapshot(payload.get("raw", {}))
        if snapshot is None:
            return None
        captured_at = _iso(payload.get("discovered_at"))
        if captured_at is None:
            return None
        historical = "latest_discovery_before_recording; configuration_at_intervention_not_verified"
        if performed_at is not None and datetime.fromisoformat(captured_at) > performed_at:
            historical = "captured_after_reported_intervention; historical_configuration_unknown"
        return {"value": snapshot, "fingerprint": snapshot_fingerprint(snapshot),
                "captured_at": captured_at, "source": "zont:discover.read_only.z3k_config",
                "historical_context": historical}

    def set_recommendation_feedback(
        self, recommendation_id: str, status: str, owner_note: str | None = None,
        experiment: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"applied", "rejected"}:
            raise ValueError("status must be applied or rejected")
        if status == "rejected" and experiment is not None:
            raise ValueError("experiment can only be recorded with applied feedback")
        note = (owner_note or "").strip()
        try:
            supplied = Experiment.model_validate(experiment) if experiment is not None else None
        except Exception as exc:
            raise ValueError(str(exc)) from exc

        def save(tx: Transaction) -> dict[str, Any]:
            row = _first(tx.execute(_GET_REC, {"$id": recommendation_id}))
            if row is None:
                raise KeyError(recommendation_id)
            previous = self._latest_intervention(tx, recommendation_id) if row.status == "applied" else None
            previous_experiment = self._experiment(tx, previous["id"] if previous else None)
            current_note = previous.get("note", "") if previous else _text(row.note) or ""
            supplied_value = supplied.storage_value() if supplied else None
            current_value = (Experiment.model_validate({key: value for key, value in previous_experiment.items()
                             if key != "control_snapshot" and value is not None}).storage_value()
                             if previous_experiment else None)
            if (row.status == status and current_note == note
                    and (experiment is None or current_value == supplied_value)):
                return self._view(tx, row)
            updated_at = max(self.clock(), int(row.updated_at) + 1)
            self._put_rec(tx, row, status, None if status == "applied" else note, updated_at)
            if status == "applied":
                intervention_id = f"intervention:{uuid.uuid4()}"
                tx.execute("DECLARE $id AS Utf8; DECLARE $rec AS Utf8; DECLARE $payload AS Utf8; "
                           "DECLARE $at AS Int64; UPSERT INTO interventions "
                           "(id,recommendation_id,applied_at,payload) VALUES ($id,$rec,$at,$payload);",
                           {"$id": intervention_id, "$rec": recommendation_id,
                            "$at": updated_at,
                            "$payload": _json({"id": intervention_id, "recommendation_id": recommendation_id,
                                               "note": note, "applied_at": updated_at})})
                recorded = supplied
                if recorded is None and previous_experiment is not None:
                    recorded = Experiment.model_validate({
                        key: value for key, value in previous_experiment.items()
                        if key in {"category", "parameter", "before", "after", "performed_at"}
                    })
                if recorded is not None:
                    snapshot = self._control_snapshot(tx, str(row.report_id), recorded.performed_at) if supplied else (
                        previous_experiment.get("control_snapshot") if previous_experiment else None
                    )
                    experiment_id = f"experiment:{uuid.uuid4()}"
                    tx.execute("DECLARE $id AS Utf8; DECLARE $intervention AS Utf8; DECLARE $payload AS Utf8; "
                               "UPSERT INTO intervention_experiments (id,intervention_id,payload) "
                               "VALUES ($id,$intervention,$payload);",
                               {"$id": experiment_id, "$intervention": intervention_id,
                                "$payload": _json({**recorded.storage_value(), "control_snapshot": snapshot})})
                self._capture_prediction(tx, row, intervention_id, previous, updated_at)
            self._audit(tx, recommendation_id, status, note, supplied_value, updated_at)
            self._queue_render(tx, str(row.report_id))
            refreshed = _first(tx.execute(_GET_REC, {"$id": recommendation_id}))
            return self._view(tx, refreshed)

        return self.db.transaction(save)

    @classmethod
    def _capture_prediction(
        cls, tx: Transaction, row: Any, intervention_id: str,
        previous: dict[str, Any] | None, at: int,
    ) -> None:
        key = f"intervention-prediction:{intervention_id}"
        old_key = f"intervention-prediction:{previous['id']}" if previous else None
        old = (_first(tx.execute("DECLARE $key AS Utf8; SELECT value FROM app_meta WHERE key=$key;",
                                 {"$key": old_key})) if old_key else None)
        if old is not None:
            value = _text(old.value)
        else:
            report = cls._report_payload(tx, str(row.report_id))
            if report is None:
                return
            raw_context = report.get("context")
            context = raw_context if isinstance(raw_context, dict) else {}
            rec = json.loads(_text(row.payload) or "{}")
            value = _json({"report_id": row.report_id, "generated_at": report.get("generated_at"),
                           "captured_at": _iso(at), "predictions": report.get("predictions", []),
                           "hypothesis": rec.get("hypothesis"), "expected_effect": rec.get("expected_effect"),
                           "gas": context.get("gas"), "gas_savings": context.get("gas_savings"),
                           "epistemic_level": "previous_ai_interpretation"})
        tx.execute("DECLARE $key AS Utf8; DECLARE $value AS Utf8; "
                   "UPSERT INTO app_meta (key,value) VALUES ($key,$value);",
                   {"$key": key, "$value": value})

    @staticmethod
    def _audit(tx: Transaction, recommendation_id: str, status: str, note: str,
               experiment: dict[str, Any] | None, at: int) -> None:
        row = _first(tx.execute("SELECT revision FROM revisions WHERE scope='recommendation_audit';"))
        ordinal = int(row.revision) + 1 if row else 1
        tx.execute("DECLARE $value AS Int64; UPSERT INTO revisions (scope,revision) "
                   "VALUES ('recommendation_audit',$value);", {"$value": ordinal})
        tx.execute("DECLARE $id AS Int64; DECLARE $rec AS Utf8; DECLARE $at AS Int64; "
                   "DECLARE $payload AS Utf8; UPSERT INTO recommendation_audit "
                   "(id,recommendation_id,at,payload) VALUES ($id,$rec,$at,$payload);",
                   {"$id": ordinal, "$rec": recommendation_id, "$at": at,
                    "$payload": _json({"status": status, "note": note, "experiment": experiment})})

    @staticmethod
    def _queue_render(tx: Transaction, report_id: str) -> None:
        row = _first(tx.execute("SELECT revision FROM revisions WHERE scope='publication';"))
        revision = int(row.revision) + 1 if row else 1
        tx.execute("DECLARE $value AS Int64; UPSERT INTO revisions (scope,revision) "
                   "VALUES ('publication',$value);", {"$value": revision})
        tx.execute("DECLARE $id AS Utf8; DECLARE $revision AS Int64; DECLARE $payload AS Utf8; "
                   "UPSERT INTO publication_changes (scope,identifier,revision,payload) "
                   "VALUES ('render',$id,$revision,$payload);",
                   {"$id": report_id, "$revision": revision, "$payload": _json({"report_id": report_id})})

    def intervention_history(self, limit: int = 10, *, before: datetime | None = None) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        def read(tx: Transaction) -> list[dict[str, Any]]:
            rows = tx.execute("SELECT id,payload FROM interventions VIEW by_applied_at "
                              "ORDER BY applied_at DESC,id DESC LIMIT 1000;")[0].rows
            interventions = [json.loads(_text(row.payload) or "{}") for row in rows]
            interventions.sort(key=lambda item: (int(item["applied_at"]) if isinstance(item["applied_at"], int)
                                                 else _micros(datetime.fromisoformat(_iso(item["applied_at"]) or "")),
                                                 item["id"]), reverse=True)
            history: list[dict[str, Any]] = []
            seen: set[tuple[str, str]] = set()
            for item in interventions:
                experiment = self._experiment(tx, item["id"])
                boundary = experiment.get("performed_at") if experiment else None
                boundary = boundary or _iso(item["applied_at"])
                if boundary is None or (
                    before is not None and datetime.fromisoformat(boundary) >= before.astimezone(UTC)
                ):
                    continue
                if experiment is not None:
                    identity = _json({key: value for key, value in experiment.items() if key != "control_snapshot"})
                    pair = (item["recommendation_id"], identity)
                    if pair in seen:
                        continue
                    seen.add(pair)
                history.append({"intervention_id": item["id"], "recommendation_id": item["recommendation_id"],
                                "recorded_at": _iso(item["applied_at"]), "owner_note": item["note"],
                                "experiment": experiment, "temporal_boundary": boundary})
                if len(history) == limit:
                    break
            return history
        return self.db.transaction(read)

    def mark_applied(self, recommendation_id: str, note: str) -> str:
        value = self.set_recommendation_feedback(recommendation_id, "applied", note)
        intervention_id = value.get("intervention_id")
        if not isinstance(intervention_id, str):
            raise RuntimeError(f"Applied recommendation {recommendation_id} has no intervention")
        return intervention_id

    def reject(self, recommendation_id: str, reason: str) -> None:
        self.set_recommendation_feedback(recommendation_id, "rejected", reason)

    def flush_log_outbox(self) -> list[str]:
        def flush(tx: Transaction) -> list[str]:
            rows = tx.execute("SELECT id,payload,attempts FROM notification_outbox "
                              "WHERE channel='log' AND state='pending';")[0].rows
            for row in rows:
                tx.execute("DECLARE $id AS Utf8; DECLARE $attempts AS Int64; "
                           "UPDATE notification_outbox SET state='delivered',attempts=$attempts WHERE id=$id;",
                           {"$id": row.id, "$attempts": int(row.attempts or 0) + 1})
            return [_text(row.payload) or "" for row in rows]
        return self.db.transaction(flush)
