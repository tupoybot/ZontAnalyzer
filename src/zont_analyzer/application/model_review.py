"""Persisted, bounded review of public model information on YDB.

Network catalog reads happen outside retried database transactions. No model is
changed automatically; accepting a proposal requires an owner decision.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Protocol

from zont_analyzer.adapters.openai.model_catalog import CatalogSnapshot, ModelFact
from zont_analyzer.adapters.ydb.model_settings import ModelSettingsStorage, ModelSettingsTransaction

if TYPE_CHECKING:
    from zont_analyzer.adapters.ydb.application import Database

DEFAULT_INTERVAL_DAYS = 60
MAX_RETRIES = 3
LEASE_SECONDS = 600
SCOPE = "installation"


class Catalog(Protocol):
    def fetch(self, now: datetime | None = None, model_ids: tuple[str, ...] = ()) -> CatalogSnapshot: ...


class SettingsStore(Protocol):
    def snapshot(self) -> dict[str, Any]: ...
    def snapshot_in_transaction(self, session: ModelSettingsTransaction) -> dict[str, Any]: ...
    def save(self, payload: dict[str, Any], *, session: ModelSettingsTransaction | None = None) -> dict[str, Any]: ...


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(UTC)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _iso(value: datetime | None) -> str | None:
    return _utc(value).isoformat() if value else None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).astimezone(UTC) if value else None


def _micros(value: datetime) -> int:
    utc = _utc(value)
    return int(utc.timestamp()) * 1_000_000 + utc.microsecond


class ModelReviewStore:
    def __init__(
        self, db: Database, catalog: Catalog, *, lease_seconds: int = LEASE_SECONDS,
        assessments: dict[str, Any] | None = None,
    ) -> None:
        self.db, self.catalog, self.lease_seconds = db, catalog, lease_seconds
        self.assessments = assessments or {}
        self.storage = ModelSettingsStorage(db.storage)

    @staticmethod
    def _state_item(state: dict[str, Any] | None) -> dict[str, Any]:
        if state is None:
            return {
                "last_success_at": None, "next_due_at": None, "last_attempt_at": None,
                "attempts": 0, "last_error": None, "running": False,
            }
        lease_until = _parse(state.get("lease_until"))
        return {
            "last_success_at": state.get("last_success_at"),
            "next_due_at": state.get("next_due_at"),
            "last_attempt_at": state.get("last_attempt_at"),
            "attempts": state.get("attempts", 0),
            "last_error": state.get("last_error"),
            "running": bool(lease_until and lease_until > _utc()),
        }

    @staticmethod
    def _run_item(run: dict[str, Any]) -> dict[str, Any]:
        return {key: run.get(key) for key in (
            "id", "started_at", "finished_at", "trigger", "status",
            "settings_version", "sources", "result", "error",
        )}

    @staticmethod
    def _proposal_item(proposal: dict[str, Any]) -> dict[str, Any]:
        return {key: proposal.get(key) for key in (
            "id", "run_id", "status", "settings_version", "profile",
            "current_model", "candidate_model", "recommendation",
            "created_at", "decided_at", "decision_note", "version",
        )}

    def state(self, settings_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        def read(tx: ModelSettingsTransaction) -> dict[str, Any]:
            state_item = tx.state(SCOPE)
            state = state_item[0] if state_item else None
            runs = tx.runs(SCOPE, limit=10)
            proposals = tx.proposals(limit=1000)
            settings_version = str(settings_snapshot.get("version", "")) if settings_snapshot else None
            for proposal in proposals:
                if (settings_version is None or proposal["status"] not in ("open", "deferred")
                        or proposal["settings_version"] != settings_version):
                    continue
                recommendation = proposal["recommendation"]
                if (recommendation.get("reason") != "current_model_deprecated"
                        and not (
                            isinstance(recommendation.get("evaluation"), dict)
                            and isinstance(recommendation.get("current_evaluation"), dict)
                            and self._assessments_comparable(
                                recommendation["current_evaluation"], recommendation["evaluation"],
                            )
                        )):
                    proposal["status"] = "superseded"
                    proposal["decided_at"] = _iso(_utc())
                    proposal["version"] += 1
                    tx.put_proposal(proposal)
            visible = [proposal for proposal in proposals if proposal["status"] in ("open", "deferred")]
            visible.sort(key=lambda item: item["created_at"], reverse=True)
            result = {
                **self._state_item(state),
                "runs": [self._run_item(run) for run in runs],
                "proposals": [self._proposal_item(proposal) for proposal in visible],
            }
            if settings_snapshot is not None and state is not None:
                result["next_due_at"] = _iso(self._next_due(state, settings_snapshot.get("effective", {})))
            if settings_snapshot is not None:
                result["proposals"] = [item for item in result["proposals"]
                                       if item["settings_version"] == settings_snapshot["version"]]
                result["settings_changed"] = bool(
                    runs and runs[0]["settings_version"] != settings_snapshot["version"]
                )
            return result

        return self.storage.transaction(read)

    def due(self, settings_snapshot: dict[str, Any], now: datetime | None = None) -> bool:
        effective = settings_snapshot.get("effective", {})
        if not effective.get("review_enabled", True):
            return False
        moment = _utc(now)

        def read(tx: ModelSettingsTransaction) -> bool:
            item = tx.state(SCOPE)
            next_due = self._next_due(item[0], effective) if item else None
            return next_due is None or next_due <= moment

        return self.storage.transaction(read)

    @staticmethod
    def _next_due(state: dict[str, Any], effective: dict[str, Any]) -> datetime | None:
        if state.get("last_error") and state.get("next_due_at"):
            return _parse(state["next_due_at"])
        interval = effective.get("review_interval_days", DEFAULT_INTERVAL_DAYS)
        if state.get("last_success_at"):
            last_success = _parse(state["last_success_at"])
            return last_success + timedelta(days=int(interval)) if last_success else None
        return _parse(state.get("next_due_at"))

    def _claim(self, settings: dict[str, Any], now: datetime, trigger: str) -> tuple[str, str] | None:
        effective = settings.get("effective", {})
        if trigger == "scheduled" and not effective.get("review_enabled", True):
            return None
        token, run_id = str(uuid.uuid4()), str(uuid.uuid4())

        def claim(tx: ModelSettingsTransaction) -> tuple[str, str] | None:
            item = tx.state(SCOPE)
            state, version = item if item else ({}, 0)
            lease_until = _parse(state.get("lease_until"))
            if lease_until and lease_until > now:
                return None
            next_due = self._next_due(state, effective)
            if trigger == "scheduled" and next_due is not None and next_due > now:
                return None
            for run in tx.runs(SCOPE, limit=1000):
                if run["status"] == "running":
                    run.update(status="interrupted", finished_at=_iso(now),
                               error="Проверка прервана перезапуском; выполняется повтор.")
                    tx.put_run(run)
            state.update(
                lease_token=token, lease_until=_iso(now + timedelta(seconds=self.lease_seconds)),
                last_attempt_at=_iso(now), attempts=int(state.get("attempts", 0)) + 1,
                updated_at=_iso(now),
            )
            tx.put_state(SCOPE, state, version + 1)
            tx.put_run({
                "id": run_id, "scope": SCOPE, "started_at": _iso(now),
                "started_at_us": _micros(now), "finished_at": None, "trigger": trigger,
                "status": "running", "settings_version": str(settings.get("version", "")),
                "settings": settings, "sources": [], "catalog": {}, "result": {}, "error": None,
            })
            return token, run_id

        return self.storage.transaction(claim)

    def _assessment(self, model: str, effort: str | None) -> dict[str, Any] | None:
        assessment = self.assessments.get(model)
        if not isinstance(assessment, dict):
            return None
        measurements = assessment.get("measurements")
        parameters = measurements.get("parameters") if isinstance(measurements, dict) else None
        scores = assessment.get("scores")
        completed_case_ids = assessment.get("completed_case_ids")
        if (
            not all(isinstance(assessment.get(key), str) and assessment[key].strip()
                    for key in ("dataset_sha256", "prompt_id", "schema_id", "assessor", "date", "rationale"))
            or not isinstance(completed_case_ids, list)
            or not completed_case_ids
            or any(not isinstance(case_id, str) or not case_id.strip() for case_id in completed_case_ids)
            or len(set(completed_case_ids)) != len(completed_case_ids)
            or not isinstance(parameters, dict)
            or parameters.get("reasoning_effort") != effort
            or not isinstance(scores, dict)
            or any(
                not isinstance(scores.get(dimension), (int, float))
                or isinstance(scores.get(dimension), bool)
                or not 0 <= scores[dimension] <= 1
                for dimension in ("factual", "advice", "uncertainty")
            )
        ):
            return None
        return dict(assessment)

    @staticmethod
    def _assessments_comparable(old: dict[str, Any], new: dict[str, Any]) -> bool:
        # The loader normally guarantees these identities. Keep the guard at
        # the selection boundary too because callers may provide assessments
        # directly to this store.
        for key in ("dataset_sha256", "prompt_id", "schema_id"):
            if old.get(key) != new.get(key):
                return False
        old_measurements = old.get("measurements")
        new_measurements = new.get("measurements")
        if not isinstance(old_measurements, dict) or not isinstance(new_measurements, dict):
            return False
        if old_measurements.get("parameters") != new_measurements.get("parameters"):
            return False
        old_cases, new_cases = old.get("completed_case_ids"), new.get("completed_case_ids")
        return isinstance(old_cases, list) and isinstance(new_cases, list) and set(old_cases) == set(new_cases)

    def _candidate(self, current: ModelFact, candidates: list[ModelFact], effort: str | None) -> ModelFact | None:
        if current.input_price_per_mtok_usd is None or current.output_price_per_mtok_usd is None:
            return None
        for item in candidates:
            if (
                item.id == current.id
                or item.deprecated
                or item.responses_supported is not True
                or item.structured_outputs_supported is not True
            ):
                continue
            if effort is not None and effort not in item.reasoning_efforts:
                continue
            if item.input_price_per_mtok_usd is None or item.output_price_per_mtok_usd is None:
                continue
            try:
                cost_in = Decimal(item.input_price_per_mtok_usd)
                cost_out = Decimal(item.output_price_per_mtok_usd)
                base_in = Decimal(current.input_price_per_mtok_usd)
                base_out = Decimal(current.output_price_per_mtok_usd)
                if not all(value.is_finite() and value >= 0 for value in (cost_in, cost_out, base_in, base_out)):
                    continue
            except InvalidOperation:
                continue
            old_evaluation, new_evaluation = self._assessment(current.id, effort), self._assessment(item.id, effort)
            quality_comparable = False
            improved_quality = False
            if old_evaluation and new_evaluation and self._assessments_comparable(old_evaluation, new_evaluation):
                old_scores, new_scores = old_evaluation["scores"], new_evaluation["scores"]
                dimensions = ("factual", "advice", "uncertainty")
                quality_comparable = all(new_scores[key] >= old_scores[key] for key in dimensions)
                improved_quality = quality_comparable and any(
                    new_scores[key] > old_scores[key] for key in dimensions
                )
            cheaper = cost_in <= base_in and cost_out <= base_out and (cost_in < base_in or cost_out < base_out)
            # Price alone is not evidence that a replacement is suitable.  A
            # non-deprecated model may be proposed only after comparable
            # package evaluations show that its quality does not regress.  A
            # deprecated current model remains the explicit exception: it
            # needs a replacement proposal even before local evaluation.
            if current.deprecated or (quality_comparable and (cheaper or improved_quality)):
                return item
        return None

    def run_if_due(
        self, settings_snapshot: dict[str, Any], now: datetime | None = None, trigger: str = "scheduled"
    ) -> dict[str, Any] | None:
        if trigger not in {"scheduled", "manual"}:
            raise ValueError("trigger must be scheduled or manual")
        moment = _utc(now)
        claimed = self._claim(settings_snapshot, moment, trigger)
        if claimed is None:
            return None
        token, run_id = claimed
        effective = settings_snapshot.get("effective", {})
        profiles = {"daily": effective.get("daily_model"), "review": effective.get("review_model")}
        requested = tuple(model for model in profiles.values() if isinstance(model, str))
        try:
            snapshot = self.catalog.fetch(moment, requested)
        except Exception as exc:  # catalog adapters must not kill the report worker
            snapshot = CatalogSnapshot(moment, (), (), incomplete=True, error=f"official catalog unavailable: {exc}")
        facts = {fact.id: fact for fact in snapshot.models}
        proposals: list[dict[str, Any]] = []
        status, error = "unverified", snapshot.error
        if not snapshot.incomplete and not error:
            status = "no_change"
            for profile, model_id in profiles.items():
                if not isinstance(model_id, str):
                    continue
                current = facts.get(model_id)
                if current is None:
                    # A public page cannot establish that an unlisted configured
                    # model remains supported or has equivalent capabilities.
                    status, error = "unverified", f"configured {profile} model is absent from the public catalog"
                    break
                if current.input_price_per_mtok_usd is None or current.output_price_per_mtok_usd is None:
                    status, error = "unverified", "Не удалось проверить опубликованную стоимость текущей модели."
                    break
                effort = effective.get(f"{profile}_reasoning_effort")
                if (current.responses_supported is not True or current.structured_outputs_supported is not True
                        or effort not in current.reasoning_efforts):
                    status, error = "unverified", "Не удалось подтвердить совместимость текущего профиля анализа."
                    break
                if any(fact.input_price_per_mtok_usd is None or fact.output_price_per_mtok_usd is None
                       for fact in snapshot.models if fact.responses_supported and fact.structured_outputs_supported):
                    status, error = "unverified", "Не удалось проверить опубликованную стоимость кандидатов."
                    break
                candidate = self._candidate(current, list(snapshot.models), effort if isinstance(effort, str) else None)
                if current.deprecated and candidate:
                    proposals.append(
                        {
                            "profile": profile,
                            "current": current,
                            "candidate": candidate,
                            "reason": "current_model_deprecated",
                        }
                    )
                elif candidate:
                    # No optimality claim: a differently priced published candidate merits an owner/eval review only.
                    proposals.append(
                        {
                            "profile": profile,
                            "current": current,
                            "candidate": candidate,
                            "reason": "published_candidate_requires_evaluation",
                        }
                    )
            if status != "unverified":
                status = "proposal" if proposals else "no_change"
        deprecated_current = any(fact.id in profiles.values() and fact.deprecated for fact in snapshot.models)
        result = {
            "checked_at": moment.isoformat(),
            "status": status,
            "requires_evaluation": bool(proposals),
            "deprecations": [{"model": fact.id, "shutdown_date": fact.deprecation_date,
                              "source": "https://developers.openai.com/api/docs/deprecations"}
                             for fact in snapshot.models if fact.id in profiles.values() and fact.deprecated],
            "message": "Проверка не завершена: по неполным публичным данным нельзя считать текущую модель оптимальной."
            if status == "unverified"
            else "Перед сменой модели нужна локальная оценка на пакетах приложения."
            if proposals
            else "Текущая модель помечена к отключению, но совместимая замена пока не подтверждена."
            if deprecated_current
            else "Нет подтверждённых сопоставимой оценкой качества оснований для смены; одной цены недостаточно.",
        }
        def finish(tx: ModelSettingsTransaction) -> dict[str, Any] | None:
            state_item = tx.state(SCOPE)
            run = tx.run(run_id)
            if state_item is None or run is None or state_item[0].get("lease_token") != token:
                return None
            state, version = state_item
            interval = effective.get("review_interval_days", DEFAULT_INTERVAL_DAYS)
            interval = interval if isinstance(interval, int) and interval > 0 else DEFAULT_INTERVAL_DAYS
            run.update(
                finished_at=_iso(moment), status=status,
                sources=list(snapshot.as_dict().get("sources", [])),
                catalog=snapshot.as_dict(), result=result, error=error,
            )
            tx.put_run(run)
            state.update(lease_token=None, lease_until=None, updated_at=_iso(moment))
            if status == "unverified":
                state["last_error"] = error or "official catalog is incomplete"
                if state["attempts"] < MAX_RETRIES:
                    state["next_due_at"] = _iso(moment + timedelta(hours=6))
                else:
                    state["next_due_at"] = _iso(moment + timedelta(days=interval))
                    state["attempts"] = 0
            else:
                state.update(
                    last_success_at=_iso(moment),
                    next_due_at=_iso(moment + timedelta(days=interval)),
                    last_error=None, attempts=0,
                )
                existing = tx.proposals(limit=1000)
                valid: set[tuple[str, str, str]] = set()
                for item in proposals:
                    current, candidate = item["current"], item["candidate"]
                    candidate_id = candidate.id if candidate else ""
                    identity = (item["profile"], current.id, candidate_id)
                    valid.add(identity)
                    if any(
                        previous["status"] in ("open", "deferred", "rejected")
                        and previous["settings_version"] == str(settings_snapshot.get("version", ""))
                        and (previous["profile"], previous["current_model"], previous["candidate_model"]) == identity
                        for previous in existing
                    ):
                        continue
                    evaluation = self._assessment(
                        candidate.id, effective.get(f"{item['profile']}_reasoning_effort"),
                    ) if candidate else None
                    current_evaluation = self._assessment(
                        current.id, effective.get(f"{item['profile']}_reasoning_effort"),
                    )
                    quality_note = "Не проверено на пакетах приложения; качество может измениться."
                    latency_note = "Не измерена на пакетах приложения."
                    if evaluation:
                        scores = evaluation["scores"]
                        quality_note = (
                            f"Локальная оценка: факты {scores['factual']:.2f}, "
                            f"уместность {scores['advice']:.2f}, неопределённость {scores['uncertainty']:.2f}. "
                            + str(evaluation["rationale"])
                        )
                        latency = evaluation.get("measurements", {}).get("latency_ms")
                        if latency is not None:
                            latency_note = f"Измерено локально: {latency} мс."
                    recommendation = {
                        "reason": item["reason"], "requires_evaluation": evaluation is None,
                        "evaluation": evaluation, "current_evaluation": current_evaluation,
                        "tradeoffs": {
                            "quality": quality_note, "latency": latency_note,
                            "price": {
                                "current": {
                                    "input_per_mtok_usd": current.input_price_per_mtok_usd,
                                    "output_per_mtok_usd": current.output_price_per_mtok_usd,
                                },
                                "candidate": {
                                    "input_per_mtok_usd": candidate.input_price_per_mtok_usd if candidate else None,
                                    "output_per_mtok_usd": candidate.output_price_per_mtok_usd if candidate else None,
                                },
                            },
                        },
                        "sources": list(snapshot.sources),
                    }
                    proposal = {
                        "id": str(uuid.uuid4()), "run_id": run_id, "status": "open",
                        "settings_version": str(settings_snapshot.get("version", "")),
                        "profile": item["profile"], "current_model": current.id,
                        "candidate_model": candidate_id, "recommendation": recommendation,
                        "created_at": _iso(moment), "decided_at": None,
                        "decision_note": None, "version": 1,
                    }
                    tx.put_proposal(proposal)
                for previous in existing:
                    if previous["status"] not in ("open", "deferred"):
                        continue
                    identity = (previous["profile"], previous["current_model"], previous["candidate_model"])
                    if (previous["settings_version"] == str(settings_snapshot.get("version", ""))
                            and identity in valid):
                        continue
                    previous.update(status="superseded", decided_at=_iso(moment), version=previous["version"] + 1)
                    tx.put_proposal(previous)
            tx.put_state(SCOPE, state, version + 1)
            return self._run_item(run)

        return self.storage.transaction(finish)

    def decide(
        self, proposal_id: str, action: str, expected_version: int,
        settings_store: SettingsStore, note: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if action not in {"accept", "reject", "defer"}:
            raise ValueError("action must be accept, reject or defer")
        moment = _utc(now)
        candidate: ModelFact | None = None
        fresh_facts: dict[str, ModelFact] = {}
        fresh: CatalogSnapshot | None = None
        if action == "accept":
            proposed = self.storage.transaction(lambda tx: tx.proposal(proposal_id))
            if proposed is None or not proposed["candidate_model"]:
                raise ValueError("proposal has no applicable replacement")
            fresh = self.catalog.fetch(moment, (proposed["current_model"], proposed["candidate_model"]))
            fresh_facts = {fact.id: fact for fact in fresh.models}
            candidate = fresh_facts.get(proposed["candidate_model"])
            if (fresh.incomplete or candidate is None or candidate.deprecated
                    or candidate.responses_supported is not True
                    or candidate.structured_outputs_supported is not True):
                raise ValueError("proposal is stale because its candidate is no longer verified")

        def decide_tx(tx: ModelSettingsTransaction) -> dict[str, Any]:
            proposal = tx.proposal(proposal_id)
            if proposal is None:
                raise KeyError(proposal_id)
            if proposal["version"] != expected_version or proposal["status"] not in ("open", "deferred"):
                raise ValueError("proposal is stale")
            if action == "accept":
                live = settings_store.snapshot_in_transaction(tx)
                if str(live.get("version", "")) != proposal["settings_version"]:
                    raise ValueError("proposal is stale because AI settings changed")
                effort = live["effective"][f"{proposal['profile']}_reasoning_effort"]
                if candidate is None or effort not in candidate.reasoning_efforts:
                    raise ValueError("Кандидат больше не поддерживает выбранную глубину рассуждения.")
                current = fresh_facts.get(proposal["current_model"])
                if current is None or self._candidate(current, [candidate], effort) is None:
                    raise ValueError("Основание предложения изменилось; выполните новую проверку моделей.")
                field = f"{proposal['profile']}_model"
                settings_store.save(
                    {"expected_version": proposal["settings_version"],
                     "values": {field: proposal["candidate_model"]}},
                    session=tx,
                )
                recommendation = dict(proposal["recommendation"])
                recommendation["acceptance_check"] = fresh.as_dict() if fresh else {}
                proposal["recommendation"] = recommendation
            proposal.update(
                status={"accept": "accepted", "reject": "rejected", "defer": "deferred"}[action],
                decided_at=_iso(moment), decision_note=note, version=proposal["version"] + 1,
            )
            tx.put_proposal(proposal)
            return self._proposal_item(proposal)

        return self.storage.transaction(decide_tx)
