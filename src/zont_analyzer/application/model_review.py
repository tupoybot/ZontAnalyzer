"""Persisted, bounded review of public OpenAI model information.

Fetching happens outside SQLite transactions.  A short lease prevents duplicate
workers; results are committed only while the lease token still belongs to this
attempt.  This module never calls OpenAI generation APIs and never changes a
model automatically.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from zont_analyzer.adapters.openai.model_catalog import CatalogSnapshot, ModelFact
from zont_analyzer.adapters.sqlite.database import Base, Database, utcnow

DEFAULT_INTERVAL_DAYS = 60
MAX_RETRIES = 3
LEASE_SECONDS = 600
SCOPE = "installation"


class ModelReviewStateRow(Base):
    __tablename__ = "model_review_state"
    scope: Mapped[str] = mapped_column(String, primary_key=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelReviewRunRow(Base):
    __tablename__ = "model_review_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    scope: Mapped[str] = mapped_column(String, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trigger: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, index=True)
    settings_version: Mapped[str] = mapped_column(String)
    settings_json: Mapped[str] = mapped_column(Text)
    sources_json: Mapped[str] = mapped_column(Text, default="[]")
    catalog_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ModelReviewProposalRow(Base):
    __tablename__ = "model_review_proposals"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("model_review_runs.id"), index=True)
    status: Mapped[str] = mapped_column(String, index=True, default="open")
    settings_version: Mapped[str] = mapped_column(String)
    profile: Mapped[str] = mapped_column(String)
    current_model: Mapped[str] = mapped_column(String)
    candidate_model: Mapped[str] = mapped_column(String)
    recommendation_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class Catalog(Protocol):
    def fetch(self, now: datetime | None = None, model_ids: tuple[str, ...] = ()) -> CatalogSnapshot: ...


class SettingsStore(Protocol):
    def snapshot(self) -> dict[str, Any]: ...

    def save(self, payload: dict[str, Any], *, session: Session | None = None) -> dict[str, Any]: ...


def _utc(value: datetime | None = None) -> datetime:
    value = value or utcnow()
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _iso(value: datetime | None) -> str | None:
    return _utc(value).isoformat() if value else None


class ModelReviewStore:
    def __init__(self, db: Database, catalog: Catalog, *, lease_seconds: int = LEASE_SECONDS,
                 assessments: dict[str, Any] | None = None):
        self.db, self.catalog, self.lease_seconds = db, catalog, lease_seconds
        self.assessments = assessments or {}

    @contextmanager
    def _write(self) -> Iterator[Session]:
        with self.db.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            session = Session(bind=connection)
            try:
                yield session
                session.flush()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                session.close()

    @staticmethod
    def _state_item(state: ModelReviewStateRow | None) -> dict[str, Any]:
        if state is None:
            return {
                "last_success_at": None,
                "next_due_at": None,
                "last_attempt_at": None,
                "attempts": 0,
                "last_error": None,
                "running": False,
            }
        return {
            "last_success_at": _iso(state.last_success_at),
            "next_due_at": _iso(state.next_due_at),
            "last_attempt_at": _iso(state.last_attempt_at),
            "attempts": state.attempts,
            "last_error": state.last_error,
            "running": bool(state.lease_until and _utc(state.lease_until) > _utc()),
        }

    def state(self, settings_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.db.session() as session:
            state = session.get(ModelReviewStateRow, SCOPE)
            runs = session.scalars(
                select(ModelReviewRunRow)
                .where(ModelReviewRunRow.scope == SCOPE)
                .order_by(ModelReviewRunRow.started_at.desc())
                .limit(10)
            ).all()
            proposals = session.scalars(
                select(ModelReviewProposalRow)
                .where(ModelReviewProposalRow.status.in_(("open", "deferred")))
                .order_by(ModelReviewProposalRow.created_at.desc())
            ).all()
            result = {
                **self._state_item(state),
                "runs": [self._run_item(row) for row in runs],
                "proposals": [self._proposal_item(row) for row in proposals],
            }
            if settings_snapshot is not None and state is not None:
                result["next_due_at"] = _iso(self._next_due(state, settings_snapshot.get("effective", {})))
            if settings_snapshot is not None:
                result["proposals"] = [item for item in result["proposals"]
                                       if item["settings_version"] == settings_snapshot["version"]]
                result["settings_changed"] = bool(runs and runs[0].settings_version != settings_snapshot["version"])
            return result

    @staticmethod
    def _run_item(row: ModelReviewRunRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "started_at": _iso(row.started_at),
            "finished_at": _iso(row.finished_at),
            "trigger": row.trigger,
            "status": row.status,
            "settings_version": row.settings_version,
            "sources": json.loads(row.sources_json),
            "result": json.loads(row.result_json),
            "error": row.error,
        }

    @staticmethod
    def _proposal_item(row: ModelReviewProposalRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "run_id": row.run_id,
            "status": row.status,
            "settings_version": row.settings_version,
            "profile": row.profile,
            "current_model": row.current_model,
            "candidate_model": row.candidate_model,
            "recommendation": json.loads(row.recommendation_json),
            "created_at": _iso(row.created_at),
            "decided_at": _iso(row.decided_at),
            "decision_note": row.decision_note,
            "version": row.version,
        }

    def due(self, settings_snapshot: dict[str, Any], now: datetime | None = None) -> bool:
        effective = settings_snapshot.get("effective", {})
        if not effective.get("review_enabled", True):
            return False
        now = _utc(now)
        with self.db.session() as session:
            state = session.get(ModelReviewStateRow, SCOPE)
            next_due = self._next_due(state, effective) if state else None
            return next_due is None or next_due <= now

    @staticmethod
    def _next_due(state: ModelReviewStateRow, effective: dict[str, Any]) -> datetime | None:
        if state.last_error and state.next_due_at:
            return _utc(state.next_due_at)
        interval = effective.get("review_interval_days", DEFAULT_INTERVAL_DAYS)
        if state.last_success_at:
            return _utc(state.last_success_at) + timedelta(days=int(interval))
        return _utc(state.next_due_at) if state.next_due_at else None

    def _claim(self, settings: dict[str, Any], now: datetime, trigger: str) -> tuple[str, str] | None:
        effective = settings.get("effective", {})
        if trigger == "scheduled" and not effective.get("review_enabled", True):
            return None
        with self._write() as session:
            state = session.get(ModelReviewStateRow, SCOPE)
            if state is None:
                state = ModelReviewStateRow(scope=SCOPE)
                session.add(state)
            if state.lease_until and _utc(state.lease_until) > now:
                return None
            next_due = self._next_due(state, effective)
            if trigger == "scheduled" and next_due is not None and next_due > now:
                return None
            session.execute(update(ModelReviewRunRow).where(
                ModelReviewRunRow.scope == SCOPE, ModelReviewRunRow.status == "running",
            ).values(status="interrupted", finished_at=now,
                     error="Проверка прервана перезапуском; выполняется повтор."))
            token, run_id = str(uuid.uuid4()), str(uuid.uuid4())
            state.lease_token, state.lease_until = token, now + timedelta(seconds=self.lease_seconds)
            state.last_attempt_at, state.attempts, state.updated_at = now, (state.attempts or 0) + 1, now
            session.add(
                ModelReviewRunRow(
                    id=run_id,
                    scope=SCOPE,
                    started_at=now,
                    trigger=trigger,
                    status="running",
                    settings_version=str(settings.get("version", "")),
                    settings_json=_json(settings),
                )
            )
            return token, run_id

    def _assessment(self, model: str, effort: str | None) -> dict[str, Any] | None:
        assessment = self.assessments.get(model)
        if assessment and assessment.get("measurements", {}).get("parameters", {}).get("reasoning_effort") == effort:
            return dict(assessment)
        return None

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
            improved_quality = False
            if old_evaluation and new_evaluation:
                old_scores, new_scores = old_evaluation["scores"], new_evaluation["scores"]
                dimensions = ("factual", "advice", "uncertainty")
                improved_quality = (all(new_scores[key] >= old_scores[key] for key in dimensions)
                                    and any(new_scores[key] > old_scores[key] for key in dimensions))
            if current.deprecated or improved_quality or (cost_in <= base_in and cost_out <= base_out
                                      and (cost_in < base_in or cost_out < base_out)):
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
                if any(fact.input_price_per_mtok_usd is None or fact.output_price_per_mtok_usd is None
                       for fact in snapshot.models if fact.responses_supported and fact.structured_outputs_supported):
                    status, error = "unverified", "Не удалось проверить опубликованную стоимость кандидатов."
                    break
                candidate = self._candidate(current, list(snapshot.models), effort if isinstance(effort, str) else None)
                if current.deprecated:
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
            else "Публичные данные не подтвердили основание для смены модели.",
        }
        with self._write() as session:
            state = session.get(ModelReviewStateRow, SCOPE)
            run = session.get(ModelReviewRunRow, run_id)
            if state is None or run is None or state.lease_token != token:
                return None
            interval = effective.get("review_interval_days", DEFAULT_INTERVAL_DAYS)
            interval = interval if isinstance(interval, int) and interval > 0 else DEFAULT_INTERVAL_DAYS
            run.finished_at, run.status, run.sources_json, run.catalog_json, run.result_json, run.error = (
                moment,
                status,
                _json(snapshot.as_dict().get("sources", [])),
                _json(snapshot.as_dict()),
                _json(result),
                error,
            )
            state.lease_token, state.lease_until, state.updated_at = None, None, moment
            if status == "unverified":
                state.last_error = error or "official catalog is incomplete"
                if state.attempts < MAX_RETRIES:
                    state.next_due_at = moment + timedelta(hours=6)
                else:
                    state.next_due_at = moment + timedelta(days=interval)
                    state.attempts = 0
            else:
                state.last_success_at, state.next_due_at, state.last_error, state.attempts = (
                    moment,
                    moment + timedelta(days=interval),
                    None,
                    0,
                )
                for item in proposals:
                    current, candidate = item["current"], item["candidate"]
                    candidate_id = candidate.id if candidate else ""
                    existing = session.scalar(
                        select(ModelReviewProposalRow).where(
                            ModelReviewProposalRow.status.in_(("open", "deferred", "rejected")),
                            ModelReviewProposalRow.settings_version == str(settings_snapshot.get("version", "")),
                            ModelReviewProposalRow.profile == item["profile"],
                            ModelReviewProposalRow.current_model == current.id,
                            ModelReviewProposalRow.candidate_model == candidate_id,
                        )
                    )
                    if existing is not None:
                        continue
                    # A changed candidate supersedes an undecided predecessor for
                    # the same profile, so repeated runs never multiply notices.
                    for previous in session.scalars(
                        select(ModelReviewProposalRow).where(
                            ModelReviewProposalRow.status.in_(("open", "deferred")),
                            ModelReviewProposalRow.profile == item["profile"],
                        )
                    ):
                        previous.status = "superseded"
                        previous.decided_at = moment
                    if candidate is None:
                        recommendation = {
                            "reason": item["reason"],
                            "requires_evaluation": True,
                            "replacement_available": False,
                            "sources": list(snapshot.sources),
                        }
                    else:
                        evaluation = self._assessment(
                            candidate.id, effective.get(f"{item['profile']}_reasoning_effort"),
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
                            "reason": item["reason"],
                            "requires_evaluation": evaluation is None,
                            "evaluation": evaluation,
                            "tradeoffs": {
                                "quality": quality_note,
                                "latency": latency_note,
                                "price": {
                                    "current": {
                                        "input_per_mtok_usd": current.input_price_per_mtok_usd,
                                        "output_per_mtok_usd": current.output_price_per_mtok_usd,
                                    },
                                    "candidate": {
                                        "input_per_mtok_usd": candidate.input_price_per_mtok_usd,
                                        "output_per_mtok_usd": candidate.output_price_per_mtok_usd,
                                    },
                                },
                            },
                            "sources": list(snapshot.sources),
                        }
                    session.add(
                        ModelReviewProposalRow(
                            id=str(uuid.uuid4()),
                            run_id=run_id,
                            settings_version=str(settings_snapshot.get("version", "")),
                            profile=item["profile"],
                            current_model=current.id,
                            candidate_model=candidate_id,
                            recommendation_json=_json(recommendation),
                        )
                    )
            return self._run_item(run)

    def decide(
        self,
        proposal_id: str,
        action: str,
        expected_version: int,
        settings_store: SettingsStore,
        note: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if action not in {"accept", "reject", "defer"}:
            raise ValueError("action must be accept, reject or defer")
        moment = _utc(now)
        # The network check happens before the short database transaction.  It is
        # intentionally a guard, not a discovery pass: accepting cannot revive a
        # candidate which is no longer publicly compatible or is now deprecated.
        if action == "accept":
            with self.db.session() as read_session:
                proposed = read_session.get(ModelReviewProposalRow, proposal_id)
                if proposed is None or not proposed.candidate_model:
                    raise ValueError("proposal has no applicable replacement")
                current_model, candidate_model = proposed.current_model, proposed.candidate_model
            fresh = self.catalog.fetch(moment, (current_model, candidate_model))
            fresh_facts = {fact.id: fact for fact in fresh.models}
            candidate = fresh_facts.get(candidate_model)
            if (
                fresh.incomplete
                or candidate is None
                or candidate.deprecated
                or candidate.responses_supported is not True
                or candidate.structured_outputs_supported is not True
            ):
                raise ValueError("proposal is stale because its candidate is no longer verified")
        with self._write() as session:
            proposal = session.get(ModelReviewProposalRow, proposal_id)
            if proposal is None:
                raise KeyError(proposal_id)
            if proposal.version != expected_version or proposal.status not in {"open", "deferred"}:
                raise ValueError("proposal is stale")
            if action == "accept":
                live = settings_store.snapshot()
                if str(live.get("version", "")) != proposal.settings_version:
                    raise ValueError("proposal is stale because AI settings changed")
                effort = live["effective"][f"{proposal.profile}_reasoning_effort"]
                if candidate is None or effort not in candidate.reasoning_efforts:
                    raise ValueError("Кандидат больше не поддерживает выбранную глубину рассуждения.")
                current = fresh_facts.get(proposal.current_model)
                if current is None or self._candidate(current, [candidate], effort) is None:
                    raise ValueError("Основание предложения изменилось; выполните новую проверку моделей.")
                field = f"{proposal.profile}_model"
                settings_store.save(
                    {"expected_version": proposal.settings_version, "values": {field: proposal.candidate_model}},
                    session=session,
                )
                recommendation = json.loads(proposal.recommendation_json)
                recommendation["acceptance_check"] = fresh.as_dict()
                proposal.recommendation_json = _json(recommendation)
            proposal.status, proposal.decided_at, proposal.decision_note, proposal.version = (
                {"accept": "accepted", "reject": "rejected", "defer": "deferred"}[action],
                moment,
                note,
                proposal.version + 1,
            )
            return self._proposal_item(proposal)
