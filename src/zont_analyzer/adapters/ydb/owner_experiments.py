"""Owner journal: transactional revisions, frozen initial intent and explicit event time."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from zont_analyzer.domain.experiments import OwnerExperiment

from .feedback import FeedbackRepository, Transaction, _first, _iso, _json, _text


class OwnerExperimentsRepository(FeedbackRepository):
    def experiments_for_report(self, report_id: str) -> dict[str, Any]:
        def read(tx: Transaction) -> dict[str, Any]:
            report = self._report_payload(tx, report_id)
            if report is None:
                raise KeyError(report_id)
            devices = self._devices(tx, report)
            rows = tx.execute("DECLARE $report AS Utf8; SELECT payload FROM owner_experiments "
                              "WHERE report_id=$report;", {"$report": report_id})[0].rows
            return {"report_id": report_id, "timezone": report.get("timezone", "UTC"),
                    "devices": devices, "experiments": [json.loads(_text(row.payload) or "{}") for row in rows]}
        return self.db.transaction(read)

    @staticmethod
    def _devices(tx: Transaction, report: dict[str, Any]) -> list[dict[str, Any]]:
        context = report.get("context") or {}
        binding = (context.get("control_settings_history") or {}).get("device_id")
        sensors = context.get("sensors") or {}
        control = sensors.get("control_temperature") or {}
        selected = {str(binding or control.get("device_id"))} if binding or control.get("device_id") else {
            str(profile["device_id"]) for profile in context.get("equipment_profiles", [])
            if profile.get("device_id")
        }
        def sensor_devices(value: Any) -> set[str]:
            if isinstance(value, dict):
                identifiers = {str(value["device_id"])} if value.get("device_id") else set()
                return identifiers.union(*(sensor_devices(child) for child in value.values()))
            if isinstance(value, list):
                return set().union(*(sensor_devices(child) for child in value))
            return set()

        if not selected:
            selected = sensor_devices(sensors)
        if not selected and context.get("device_id"):
            selected = {str(context["device_id"])}
        rows = tx.execute("SELECT id,payload FROM devices;")[0].rows
        result = []
        for row in rows:
            identifier = str(row.id)
            if selected and identifier not in selected:
                continue
            device = json.loads(_text(row.payload) or "{}")
            config = (device.get("raw") or {}).get("z3k_config") or {}
            circuits = config.get("heating_circuits", config.get("heating_circuit", []))
            if isinstance(circuits, dict):
                circuits = list(circuits.values())
            result.append({"id": identifier, "circuits": [str(c["id"]) for c in circuits
                           if isinstance(c, dict) and c.get("id") is not None]})
        # Older reports without a device scope are safe only with one discovered device.
        return result if selected or len(result) == 1 else []

    def save_owner_experiment(self, report_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        supplied = OwnerExperiment.model_validate(payload)
        normalized = supplied.model_dump(mode="json")
        identifier = supplied.experiment_id or supplied.request_id

        def save(tx: Transaction) -> dict[str, Any]:
            report = self._report_payload(tx, report_id)
            if report is None:
                raise KeyError(report_id)
            old = _first(tx.execute("DECLARE $id AS Utf8; SELECT payload FROM owner_experiments WHERE id=$id;",
                                    {"$id": identifier}))
            previous = json.loads(_text(old.payload) or "{}") if old else None
            if supplied.experiment_id is not None and previous is None:
                raise KeyError(supplied.experiment_id)
            if previous and previous["report_id"] != report_id:
                raise ValueError("Эксперимент не принадлежит отчёту.")
            history = previous["history"] if previous else []
            for event in history:
                if event["request_id"] == supplied.request_id:
                    if event["request"] != normalized:
                        raise ValueError("Повторный идентификатор запроса содержит другие данные.")
                    return previous or {}
            devices = {d["id"]: d["circuits"] for d in self._devices(tx, report)}
            for change in supplied.changes:
                if change.device_id not in devices or (
                    change.circuit_id is not None and change.circuit_id not in devices[change.device_id]
                ):
                    raise ValueError("Устройство или контур не принадлежит отчёту.")
            if supplied.recommendation_id is not None:
                rec = _first(tx.execute("DECLARE $id AS Utf8; SELECT report_id FROM recommendations WHERE id=$id;",
                                       {"$id": supplied.recommendation_id}))
                if rec is None or str(rec.report_id) != report_id:
                    raise ValueError("Рекомендация не принадлежит отчёту.")
            revision = previous["revision"] if previous else 0
            if supplied.expected_revision != revision:
                raise ValueError("Запись изменена. Загрузите актуальную версию перед сохранением.")
            if previous and supplied.recommendation_id != previous["recommendation_id"]:
                raise ValueError("Связь с рекомендацией нельзя менять.")
            allowed = {
                "planned": {"planned", "performed", "cancelled"},
                "performed": {"performed", "completed", "stopped", "rollback_planned", "rolled_back"},
                "completed": {"completed", "rollback_planned", "rolled_back"},
                "stopped": {"stopped", "rollback_planned", "rolled_back"},
                "rollback_planned": {"rollback_planned", "rolled_back", "stopped"},
                "rolled_back": {"rolled_back"}, "cancelled": {"cancelled"},
            }
            if previous and supplied.state not in allowed[previous["state"]]:
                raise ValueError("Недопустимый переход состояния эксперимента.")
            if not previous and supplied.state not in {"planned", "performed"}:
                raise ValueError("Начните с плана или выполненного изменения.")
            at = _iso(self.clock())
            event = {"request_id": supplied.request_id, "request": normalized, "recorded_at": at}
            value = {**normalized, "id": identifier, "report_id": report_id,
                     "revision": revision + 1, "recorded_at": previous["recorded_at"] if previous else at,
                     "updated_at": at,
                     "confirmation": "intent_only" if supplied.state in {"planned", "cancelled"} else "owner_reported",
                     "telemetry_confirmation": "not_verified",
                     "original": previous["original"] if previous else {
                         "goal": supplied.goal, "expected_effect": supplied.expected_effect,
                         "observation_window": supplied.observation_window,
                         "predictions": report.get("predictions", []), "generated_at": report.get("generated_at")},
                     "history": [*history, event]}
            tx.execute("DECLARE $id AS Utf8; DECLARE $report AS Utf8; DECLARE $payload AS Utf8; "
                       "UPSERT INTO owner_experiments (id,report_id,payload) VALUES ($id,$report,$payload);",
                       {"$id": identifier, "$report": report_id, "$payload": _json(value)})
            self._queue_render(tx, report_id)
            return value
        return self.db.transaction(save)

    def factual_history(self, *, before: Any = None) -> list[dict[str, Any]]:
        def read(tx: Transaction) -> list[dict[str, Any]]:
            rows = tx.execute("SELECT payload FROM owner_experiments;")[0].rows
            result = []
            for row in rows:
                value = json.loads(_text(row.payload) or "{}")
                when = value.get("performed_at")
                if value["state"] in {"planned", "cancelled"}:
                    continue
                experiment = {key: val for key, val in value.items()
                              if key not in {"history", "request_id", "expected_revision"}}
                events = [(value["id"], when, {**experiment, "event": "owner_reported_change",
                                             "state": "performed", "lifecycle_state": value["state"]})] if when else []
                if value["state"] == "rolled_back" and value.get("rollback_at"):
                    events.append((value["id"] + ":rollback", value["rollback_at"],
                                   {**experiment, "event": "owner_reported_rollback", "state": "rolled_back",
                                    "category": "firmware_rollback" if value["category"].startswith("firmware_")
                                    else value["category"],
                                    "changes": [{**change, "before": change["after"], "after": change["before"]}
                                                for change in value["changes"]],
                                    "original_performed_at": when, "performed_at": value["rollback_at"]}))
                for identifier, boundary, detail in events:
                    if before is not None and datetime.fromisoformat(boundary) >= before.astimezone(UTC):
                        continue
                    result.append({"intervention_id": identifier, "recommendation_id": value["recommendation_id"],
                                   "recorded_at": value["recorded_at"], "owner_note": value["comment"],
                                   "experiment": detail, "temporal_boundary": boundary})
            return result
        return self.db.transaction(read)
