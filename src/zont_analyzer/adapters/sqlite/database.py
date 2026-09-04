from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    func,
    inspect,
    select,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from zont_analyzer.domain import Report, SourceEvent, TelemetryPoint


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


@dataclass(frozen=True)
class MigrationResult:
    previous_revision: str | None
    revision: str
    backup_path: Path | None = None
    adopted_legacy_schema: bool = False


class DeviceRow(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, default="")
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_json: Mapped[str] = mapped_column(Text)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EntityRow(Base):
    __tablename__ = "entities"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    source_type: Mapped[str] = mapped_column(String)
    external_id: Mapped[str] = mapped_column(String)
    display_name: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String, default="unknown")
    unit: Mapped[str | None] = mapped_column(String, nullable=True)
    provenance: Mapped[str] = mapped_column(String, default="zont")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    __table_args__ = (UniqueConstraint("device_id", "source_type", "external_id"),)


class ConfigSnapshotRow(Base):
    __tablename__ = "config_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String, index=True)
    content_hash: Mapped[str] = mapped_column(String, unique=True)
    payload_json: Mapped[str] = mapped_column(Text)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TelemetrySeriesRow(Base):
    __tablename__ = "telemetry_series"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String, index=True)
    source_type: Mapped[str] = mapped_column(String)
    entity_id: Mapped[str] = mapped_column(String)
    metric_key: Mapped[str] = mapped_column(String)
    unit: Mapped[str | None] = mapped_column(String, nullable=True)
    display_name: Mapped[str] = mapped_column(String, default="")
    role: Mapped[str] = mapped_column(String, default="unknown")
    confidence: Mapped[float] = mapped_column(Float, default=0.3)
    provenance: Mapped[str] = mapped_column(String, default="unknown")
    origin: Mapped[str] = mapped_column(String, default="unknown")
    __table_args__ = (UniqueConstraint("device_id", "source_type", "entity_id", "metric_key"),)


class TelemetrySampleRow(Base):
    __tablename__ = "telemetry_samples"
    series_id: Mapped[int] = mapped_column(ForeignKey("telemetry_series.id", ondelete="CASCADE"), primary_key=True)
    timestamp_utc: Mapped[int] = mapped_column(Integer, primary_key=True)
    value_num: Mapped[float | None] = mapped_column(Float, nullable=True)
    value_text: Mapped[str | None] = mapped_column(String, nullable=True)
    quality: Mapped[str] = mapped_column(String, default="valid")
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index("ix_samples_time_series", "timestamp_utc", "series_id"),)


class IngestionCursorRow(Base):
    __tablename__ = "ingestion_cursors"
    device_id: Mapped[str] = mapped_column(String, primary_key=True)
    data_type: Mapped[str] = mapped_column(String, primary_key=True)
    timestamp_utc: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DataGapRow(Base):
    __tablename__ = "data_gaps"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    series_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[int] = mapped_column(Integer)
    ended_at: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String)


class SourceEventRow(Base):
    __tablename__ = "source_events"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    device_id: Mapped[str] = mapped_column(String, index=True)
    event_type: Mapped[str] = mapped_column(String, index=True)
    timestamp_utc: Mapped[int] = mapped_column(Integer, index=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    important: Mapped[bool] = mapped_column(Boolean, default=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AnalysisPeriodRow(Base):
    __tablename__ = "analysis_periods"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    started_at: Mapped[int] = mapped_column(Integer)
    ended_at: Mapped[int] = mapped_column(Integer)
    coverage: Mapped[float] = mapped_column(Float)
    algorithm_version: Mapped[str] = mapped_column(String)
    __table_args__ = (UniqueConstraint("kind", "started_at", "ended_at", "algorithm_version"),)


class MetricValueRow(Base):
    __tablename__ = "metric_values"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    period_id: Mapped[str] = mapped_column(ForeignKey("analysis_periods.id"))
    name: Mapped[str] = mapped_column(String)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String)
    algorithm_version: Mapped[str] = mapped_column(String)
    context_json: Mapped[str] = mapped_column(Text, default="{}")


class DetectedEventRow(Base):
    __tablename__ = "detected_events"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    period_id: Mapped[str] = mapped_column(ForeignKey("analysis_periods.id"))
    kind: Mapped[str] = mapped_column(String)
    started_at: Mapped[int] = mapped_column(Integer)
    ended_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    severity: Mapped[str] = mapped_column(String)
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    algorithm_version: Mapped[str] = mapped_column(String)


class ReportRow(Base):
    __tablename__ = "reports"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    period_start: Mapped[int] = mapped_column(Integer)
    period_end: Mapped[int] = mapped_column(Integer)
    canonical_json: Mapped[str] = mapped_column(Text)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    algorithm_version: Mapped[str] = mapped_column(String)
    __table_args__ = (UniqueConstraint("kind", "period_start", "period_end", "algorithm_version"),)


class RecommendationRow(Base):
    __tablename__ = "recommendations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    report_id: Mapped[str] = mapped_column(ForeignKey("reports.id"), index=True)
    category: Mapped[str] = mapped_column(String)
    priority: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="new")
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class InterventionRow(Base):
    __tablename__ = "interventions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    recommendation_id: Mapped[str] = mapped_column(ForeignKey("recommendations.id"))
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    note: Mapped[str] = mapped_column(Text)


class JobRow(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    job_type: Mapped[str] = mapped_column(String)
    idempotency_key: Mapped[str] = mapped_column(String, unique=True)
    status: Mapped[str] = mapped_column(String)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class LlmCallRow(Base):
    __tablename__ = "llm_calls"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    report_id: Mapped[str | None] = mapped_column(String, nullable=True)
    input_hash: Mapped[str] = mapped_column(String)
    prompt_version: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)
    reasoning_effort: Mapped[str] = mapped_column(String)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String)
    request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotificationOutboxRow(Base):
    __tablename__ = "notification_outbox"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    report_id: Mapped[str] = mapped_column(ForeignKey("reports.id"))
    channel: Mapped[str] = mapped_column(String)
    payload: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str] = mapped_column(String, unique=True)


class AppMetaRow(Base):
    __tablename__ = "app_meta"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Database:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{self.path}", future=True)
        self.migration_result: MigrationResult | None = None

        @event.listens_for(self.engine, "connect")
        def set_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    def initialize(self, backup_dir: Path | None = None) -> MigrationResult:
        result = self._migrate(backup_dir or self.path.parent / "backups")
        with self.session() as session:
            session.merge(AppMetaRow(key="schema_version", value=result.revision))
            if session.get(AppMetaRow, "instance_id") is None:
                session.add(AppMetaRow(key="instance_id", value=str(uuid.uuid4())))
        self.migration_result = result
        return result

    def _migration_config(self) -> Config:
        package_root = Path(__file__).resolve().parents[2]
        candidates = (package_root / "_migrations", package_root.parents[1] / "migrations")
        script_location = next((candidate for candidate in candidates if candidate.is_dir()), None)
        if script_location is None:
            raise RuntimeError("Alembic migration scripts are missing from the installation")
        config = Config()
        config.set_main_option("script_location", str(script_location))
        config.set_main_option("sqlalchemy.url", str(self.engine.url).replace("%", "%%"))
        return config

    def current_revision(self) -> str | None:
        with self.engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()

    def _run_alembic(self, config: Config, operation: str, revision: str) -> None:
        try:
            with self.engine.begin() as connection:
                config.attributes["connection"] = connection
                if operation == "stamp":
                    command.stamp(config, revision)
                else:
                    command.upgrade(config, revision)
        finally:
            config.attributes.pop("connection", None)

    @staticmethod
    def _legacy_schema_mismatches(schema_inspector: Inspector) -> list[str]:
        mismatches: list[str] = []
        for table_name, table in Base.metadata.tables.items():
            expected_columns = set(table.columns.keys())
            actual_columns = {str(column["name"]) for column in schema_inspector.get_columns(table_name)}
            if actual_columns != expected_columns:
                mismatches.append(f"{table_name} columns")
            expected_primary_key = {column.name for column in table.primary_key.columns}
            primary_key = schema_inspector.get_pk_constraint(table_name).get("constrained_columns") or []
            if {str(column) for column in primary_key} != expected_primary_key:
                mismatches.append(f"{table_name} primary key")
        return mismatches

    def _migrate(self, backup_dir: Path) -> MigrationResult:
        config = self._migration_config()
        scripts = ScriptDirectory.from_config(config)
        head = scripts.get_current_head()
        if head is None:
            raise RuntimeError("Alembic migration history has no head revision")

        with self.engine.connect() as connection:
            previous_revision = MigrationContext.configure(connection).get_current_revision()
            schema_inspector = inspect(connection)
            existing_tables = set(schema_inspector.get_table_names()) - {"alembic_version"}
            legacy_mismatches = (
                self._legacy_schema_mismatches(schema_inspector)
                if previous_revision is None and set(Base.metadata.tables) <= existing_tables
                else []
            )

        adopted_legacy_schema = False
        if previous_revision is None and existing_tables:
            expected_tables = set(Base.metadata.tables)
            missing_tables = expected_tables - existing_tables
            if missing_tables:
                missing = ", ".join(sorted(missing_tables))
                raise RuntimeError(
                    "Database has an unversioned, incomplete schema; refusing to guess a migration path. "
                    f"Missing tables: {missing}"
                )
            if legacy_mismatches:
                details = ", ".join(legacy_mismatches)
                raise RuntimeError(
                    "Database has an unversioned schema that does not match the known legacy baseline; "
                    f"refusing to stamp it. Mismatches: {details}"
                )
            adopted_legacy_schema = True
        elif previous_revision is not None:
            known_revisions = {revision.revision for revision in scripts.walk_revisions()}
            if previous_revision not in known_revisions:
                raise RuntimeError(f"Database schema revision is unknown to this application: {previous_revision}")

        needs_change = previous_revision != head
        backup_path = self.backup(backup_dir) if needs_change and existing_tables else None
        if adopted_legacy_schema:
            self._run_alembic(config, "stamp", head)
        elif needs_change:
            self._run_alembic(config, "upgrade", "head")

        revision = self.current_revision()
        if revision != head:
            raise RuntimeError(f"Database migration did not reach head revision {head}; current revision is {revision}")
        return MigrationResult(
            previous_revision=previous_revision,
            revision=revision,
            backup_path=backup_path,
            adopted_legacy_schema=adopted_legacy_schema,
        )

    @contextmanager
    def session(self) -> Iterator[Session]:
        with Session(self.engine) as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    def integrity_check(self) -> str:
        with self.engine.connect() as connection:
            return str(connection.exec_driver_sql("PRAGMA integrity_check").scalar_one())

    def save_devices(self, devices: Iterable[dict[str, Any]]) -> int:
        count = 0
        with self.session() as session:
            for device in devices:
                device_id = str(device.get("device_id") or device.get("id"))
                if not device_id or device_id == "None":
                    continue
                row = DeviceRow(
                    id=device_id,
                    name=str(device.get("name") or device.get("alias") or device_id),
                    model=str(device.get("devtype") or device.get("model") or "") or None,
                    raw_json=json.dumps(device, ensure_ascii=False, sort_keys=True),
                    discovered_at=utcnow(),
                )
                session.merge(row)
                count += 1
                encoded = row.raw_json
                digest = hashlib.sha256(encoded.encode()).hexdigest()
                statement = sqlite_insert(ConfigSnapshotRow).values(
                    device_id=device_id, content_hash=digest, payload_json=encoded, captured_at=utcnow()
                )
                session.execute(statement.on_conflict_do_nothing(index_elements=["content_hash"]))
        return count

    def list_devices(self) -> list[dict[str, Any]]:
        with self.session() as session:
            rows = session.scalars(select(DeviceRow).order_by(DeviceRow.id)).all()
            return [
                {"id": row.id, "name": row.name, "model": row.model, "raw": json.loads(row.raw_json)} for row in rows
            ]

    def upsert_entity(
        self,
        *,
        entity_id: str,
        device_id: str,
        source_type: str,
        external_id: str,
        display_name: str,
        role: str,
        unit: str | None,
        confidence: float,
        provenance: str = "zont history metadata",
    ) -> None:
        with self.session() as session:
            session.merge(
                EntityRow(
                    id=entity_id,
                    device_id=device_id,
                    source_type=source_type,
                    external_id=external_id,
                    display_name=display_name,
                    role=role,
                    unit=unit,
                    confidence=confidence,
                    provenance=provenance,
                )
            )

    def _series_id(self, session: Session, point: TelemetryPoint, role: str = "unknown") -> int:
        query = select(TelemetrySeriesRow).where(
            TelemetrySeriesRow.device_id == point.device_id,
            TelemetrySeriesRow.source_type == point.source_type,
            TelemetrySeriesRow.entity_id == point.entity_id,
            TelemetrySeriesRow.metric_key == point.metric_key,
        )
        found = session.scalar(query)
        if found is not None:
            if found.unit is None and point.unit is not None:
                found.unit = point.unit
            return found.id
        row = TelemetrySeriesRow(
            device_id=point.device_id,
            source_type=point.source_type,
            entity_id=point.entity_id,
            metric_key=point.metric_key,
            unit=point.unit,
            display_name=point.entity_id,
            role=role,
            confidence=0.3,
            provenance="history source only",
            origin=point.source_type,
        )
        session.add(row)
        session.flush()
        return row.id

    def upsert_samples(self, points: Iterable[TelemetryPoint], roles: dict[str, str] | None = None) -> int:
        roles = roles or {}
        count = 0
        with self.session() as session:
            cache: dict[tuple[str, str, str, str], int] = {}
            rows: list[dict[str, Any]] = []
            for point in points:
                key = (point.device_id, point.source_type, point.entity_id, point.metric_key)
                if key not in cache:
                    cache[key] = self._series_id(session, point, roles.get(point.entity_id, "unknown"))
                rows.append(
                    {
                        "series_id": cache[key],
                        "timestamp_utc": int(point.timestamp_utc.timestamp()),
                        "value_num": point.value_num,
                        "value_text": point.value_text,
                        "quality": point.quality,
                        "ingested_at": utcnow(),
                    }
                )
                count += 1
                if len(rows) >= 2000:
                    self._write_samples(session, rows)
                    rows.clear()
            if rows:
                self._write_samples(session, rows)
        return count

    @staticmethod
    def _write_samples(session: Session, rows: list[dict[str, Any]]) -> None:
        statement = sqlite_insert(TelemetrySampleRow).values(rows)
        session.execute(
            statement.on_conflict_do_update(
                index_elements=["series_id", "timestamp_utc"],
                set_={
                    "value_num": statement.excluded.value_num,
                    "value_text": statement.excluded.value_text,
                    "quality": statement.excluded.quality,
                    "ingested_at": statement.excluded.ingested_at,
                },
            )
        )

    def list_series(self) -> list[dict[str, Any]]:
        with self.session() as session:
            rows = session.scalars(select(TelemetrySeriesRow).order_by(TelemetrySeriesRow.id)).all()
            return [
                {
                    "id": r.id,
                    "device_id": r.device_id,
                    "source_type": r.source_type,
                    "entity_id": r.entity_id,
                    "display_name": r.display_name,
                    "metric_key": r.metric_key,
                    "unit": r.unit,
                    "role": r.role,
                    "confidence": r.confidence,
                    "provenance": r.provenance,
                    "origin": r.origin,
                }
                for r in rows
            ]

    def update_series_role(
        self,
        series_id: int,
        role: str,
        display_name: str | None = None,
        *,
        confidence: float | None = None,
        provenance: str | None = None,
        origin: str | None = None,
    ) -> None:
        with self.session() as session:
            row = session.get(TelemetrySeriesRow, series_id)
            if row:
                row.role = role
                if display_name:
                    row.display_name = display_name
                if confidence is not None:
                    row.confidence = confidence
                if provenance is not None:
                    row.provenance = provenance
                if origin is not None:
                    row.origin = origin

    def fetch_samples(self, series_id: int, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        with self.session() as session:
            query = (
                select(TelemetrySampleRow.timestamp_utc, TelemetrySampleRow.value_num)
                .where(
                    TelemetrySampleRow.series_id == series_id,
                    TelemetrySampleRow.timestamp_utc >= int(start.timestamp()),
                    TelemetrySampleRow.timestamp_utc < int(end.timestamp()),
                    TelemetrySampleRow.value_num.is_not(None),
                    TelemetrySampleRow.quality == "valid",
                )
                .order_by(TelemetrySampleRow.timestamp_utc)
            )
            return [(datetime.fromtimestamp(ts, UTC), float(value)) for ts, value in session.execute(query).all()]

    def fetch_text_samples(self, series_id: int, start: datetime, end: datetime) -> list[tuple[datetime, str]]:
        with self.session() as session:
            query = (
                select(TelemetrySampleRow.timestamp_utc, TelemetrySampleRow.value_text)
                .where(
                    TelemetrySampleRow.series_id == series_id,
                    TelemetrySampleRow.timestamp_utc >= int(start.timestamp()),
                    TelemetrySampleRow.timestamp_utc < int(end.timestamp()),
                    TelemetrySampleRow.value_text.is_not(None),
                    TelemetrySampleRow.quality == "valid",
                )
                .order_by(TelemetrySampleRow.timestamp_utc)
            )
            return [(datetime.fromtimestamp(ts, UTC), str(value)) for ts, value in session.execute(query).all()]

    def fetch_sample_timestamps(self, series_id: int, start: datetime, end: datetime) -> list[datetime]:
        with self.session() as session:
            query = (
                select(TelemetrySampleRow.timestamp_utc)
                .where(
                    TelemetrySampleRow.series_id == series_id,
                    TelemetrySampleRow.timestamp_utc >= int(start.timestamp()),
                    TelemetrySampleRow.timestamp_utc < int(end.timestamp()),
                    TelemetrySampleRow.quality == "valid",
                )
                .order_by(TelemetrySampleRow.timestamp_utc)
            )
            return [datetime.fromtimestamp(ts, UTC) for (ts,) in session.execute(query).all()]

    def upsert_source_events(self, events: Iterable[SourceEvent]) -> int:
        rows = [
            {
                "id": item.id,
                "device_id": item.device_id,
                "event_type": item.event_type,
                "timestamp_utc": int(item.timestamp_utc.timestamp()),
                "duration_seconds": item.duration_seconds,
                "details_json": json.dumps(item.details, ensure_ascii=False, sort_keys=True),
                "important": item.important,
                "ingested_at": utcnow(),
            }
            for item in events
        ]
        if not rows:
            return 0
        with self.session() as session:
            statement = sqlite_insert(SourceEventRow).values(rows)
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "duration_seconds": statement.excluded.duration_seconds,
                        "details_json": statement.excluded.details_json,
                        "important": statement.excluded.important,
                        "ingested_at": statement.excluded.ingested_at,
                    },
                )
            )
        return len(rows)

    def list_source_events(self, start: datetime, end: datetime) -> list[SourceEvent]:
        with self.session() as session:
            rows = session.scalars(
                select(SourceEventRow)
                .where(
                    SourceEventRow.timestamp_utc >= int(start.timestamp()),
                    SourceEventRow.timestamp_utc < int(end.timestamp()),
                )
                .order_by(SourceEventRow.timestamp_utc, SourceEventRow.id)
            ).all()
            return [
                SourceEvent(
                    id=row.id,
                    device_id=row.device_id,
                    event_type=row.event_type,
                    timestamp_utc=datetime.fromtimestamp(row.timestamp_utc, UTC),
                    duration_seconds=row.duration_seconds,
                    details=json.loads(row.details_json),
                    important=row.important,
                )
                for row in rows
            ]

    def latest_sample_time(self) -> datetime | None:
        with self.session() as session:
            value = session.scalar(select(func.max(TelemetrySampleRow.timestamp_utc)))
            return datetime.fromtimestamp(value, UTC) if value is not None else None

    def earliest_sample_time(self) -> datetime | None:
        with self.session() as session:
            value = session.scalar(select(func.min(TelemetrySampleRow.timestamp_utc)))
            return datetime.fromtimestamp(value, UTC) if value is not None else None

    def set_cursor(self, device_id: str, data_type: str, timestamp: datetime) -> None:
        with self.session() as session:
            statement = sqlite_insert(IngestionCursorRow).values(
                device_id=device_id,
                data_type=data_type,
                timestamp_utc=int(timestamp.timestamp()),
                updated_at=utcnow(),
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=["device_id", "data_type"],
                    set_={
                        "timestamp_utc": statement.excluded.timestamp_utc,
                        "updated_at": statement.excluded.updated_at,
                    },
                )
            )

    def get_cursor(self, device_id: str, data_type: str) -> datetime | None:
        with self.session() as session:
            row = session.get(IngestionCursorRow, (device_id, data_type))
            return datetime.fromtimestamp(row.timestamp_utc, UTC) if row else None

    def set_app_meta(self, key: str, value: str) -> None:
        with self.session() as session:
            session.merge(AppMetaRow(key=key, value=value))

    def get_app_meta(self, key: str) -> str | None:
        with self.session() as session:
            row = session.get(AppMetaRow, key)
            return row.value if row else None

    def save_report(self, report: Report, rendered_text: str) -> None:
        period_id = f"{report.kind}:{int(report.period_start.timestamp())}:{report.algorithm_version}"
        for index, recommendation in enumerate(report.recommendations):
            if recommendation.id is None:
                recommendation.id = f"rec:{report.id}:{index + 1}"
        with self.session() as session:
            session.merge(
                AnalysisPeriodRow(
                    id=period_id,
                    kind=report.kind,
                    started_at=int(report.period_start.timestamp()),
                    ended_at=int(report.period_end.timestamp()),
                    coverage=report.quality.coverage_pct,
                    algorithm_version=report.algorithm_version,
                )
            )
            for metric in report.metrics:
                session.merge(
                    MetricValueRow(
                        id=metric.id,
                        period_id=period_id,
                        name=metric.name,
                        value=metric.value,
                        unit=metric.unit,
                        algorithm_version=metric.algorithm_version,
                        context_json=json.dumps(metric.context, ensure_ascii=False),
                    )
                )
            for detected in report.events:
                session.merge(
                    DetectedEventRow(
                        id=detected.id,
                        period_id=period_id,
                        kind=detected.kind,
                        started_at=int(detected.started_at.timestamp()),
                        ended_at=int(detected.ended_at.timestamp()) if detected.ended_at else None,
                        severity=detected.severity,
                        details_json=json.dumps(detected.details, ensure_ascii=False),
                        algorithm_version=detected.algorithm_version,
                    )
                )
            session.merge(
                ReportRow(
                    id=report.id,
                    kind=report.kind,
                    period_start=int(report.period_start.timestamp()),
                    period_end=int(report.period_end.timestamp()),
                    canonical_json=report.model_dump_json(),
                    generated_at=report.generated_at,
                    algorithm_version=report.algorithm_version,
                )
            )
            for index, recommendation in enumerate(report.recommendations):
                rec_id = recommendation.id or f"rec:{report.id}:{index + 1}"
                rec_statement = sqlite_insert(RecommendationRow).values(
                    id=rec_id,
                    report_id=report.id,
                    category=recommendation.category,
                    priority=recommendation.priority,
                    status="new",
                    payload_json=recommendation.model_dump_json(),
                    created_at=utcnow(),
                    updated_at=utcnow(),
                )
                session.execute(
                    rec_statement.on_conflict_do_update(
                        index_elements=["id"],
                        set_={
                            "category": rec_statement.excluded.category,
                            "priority": rec_statement.excluded.priority,
                            "payload_json": rec_statement.excluded.payload_json,
                            "updated_at": rec_statement.excluded.updated_at,
                        },
                    )
                )
            outbox_id = f"outbox:{report.id}:log"
            statement = sqlite_insert(NotificationOutboxRow).values(
                id=outbox_id,
                report_id=report.id,
                channel="log",
                payload=rendered_text,
                status="pending",
                attempts=0,
                idempotency_key=f"report:{report.id}:log:v2",
            )
            session.execute(statement.on_conflict_do_nothing(index_elements=["idempotency_key"]))

    def report(self, report_id: str) -> Report | None:
        with self.session() as session:
            row = session.get(ReportRow, report_id)
            return Report.model_validate_json(row.canonical_json) if row else None

    def latest_report(self) -> Report | None:
        with self.session() as session:
            row = session.scalar(select(ReportRow).order_by(ReportRow.generated_at.desc()).limit(1))
            return Report.model_validate_json(row.canonical_json) if row else None

    def recommendations(self) -> list[dict[str, Any]]:
        with self.session() as session:
            rows = session.scalars(select(RecommendationRow).order_by(RecommendationRow.created_at.desc())).all()
            return [self._recommendation_view(session, row) for row in rows]

    def recommendation(self, recommendation_id: str) -> dict[str, Any] | None:
        with self.session() as session:
            row = session.get(RecommendationRow, recommendation_id)
            if not row:
                return None
            return self._recommendation_view(session, row)

    def recommendation_views_for_report(self, report_id: str) -> dict[str, dict[str, Any]]:
        """Return mutable lifecycle state keyed by recommendation ID for rendering."""
        with self.session() as session:
            rows = session.scalars(
                select(RecommendationRow)
                .where(RecommendationRow.report_id == report_id)
                .order_by(RecommendationRow.created_at)
            ).all()
            return {row.id: self._recommendation_view(session, row) for row in rows}

    @staticmethod
    def _latest_intervention(session: Session, recommendation_id: str) -> InterventionRow | None:
        return session.scalar(
            select(InterventionRow)
            .where(InterventionRow.recommendation_id == recommendation_id)
            .order_by(InterventionRow.applied_at.desc(), InterventionRow.id.desc())
            .limit(1)
        )

    @classmethod
    def _recommendation_view(cls, session: Session, row: RecommendationRow) -> dict[str, Any]:
        owner_note = row.rejection_reason
        intervention: InterventionRow | None = None
        if row.status == "applied":
            intervention = cls._latest_intervention(session, row.id)
            owner_note = intervention.note if intervention else None
        updated_at = row.updated_at if row.updated_at.tzinfo is not None else row.updated_at.replace(tzinfo=UTC)
        return {
            **json.loads(row.payload_json),
            "id": row.id,
            "status": row.status,
            "report_id": row.report_id,
            "owner_note": owner_note,
            "updated_at": updated_at.isoformat(),
            "intervention_id": intervention.id if intervention else None,
        }

    def recommendation_feedback(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return compact owner-confirmed outcomes for future analysis packets."""
        if limit < 1:
            return []
        with self.session() as session:
            rows = session.scalars(
                select(RecommendationRow)
                .where(RecommendationRow.status.in_(("applied", "rejected")))
                .order_by(RecommendationRow.updated_at.desc())
                .limit(limit)
            ).all()
            feedback: list[dict[str, Any]] = []
            for row in rows:
                payload = json.loads(row.payload_json)
                owner_note = row.rejection_reason
                if row.status == "applied":
                    intervention = self._latest_intervention(session, row.id)
                    owner_note = intervention.note if intervention else None
                feedback.append(
                    {
                        "recommendation_id": row.id,
                        "report_id": row.report_id,
                        "status": row.status,
                        "title": payload.get("title"),
                        "category": payload.get("category", row.category),
                        "hypothesis": payload.get("hypothesis"),
                        "owner_note": owner_note,
                        "updated_at": row.updated_at.isoformat(),
                    }
                )
            return feedback

    def recommendation_status_counts(self) -> dict[str, int]:
        """Return lifecycle totals, including empty states, for maintenance reporting."""
        counts = {status: 0 for status in ("new", "applied", "rejected", "ignored")}
        with self.session() as session:
            rows = session.execute(
                select(RecommendationRow.status, func.count())
                .group_by(RecommendationRow.status)
                .order_by(RecommendationRow.status)
            ).all()
        counts.update({str(status): int(count) for status, count in rows})
        return counts

    def stale_recommendation_count(self, *, now: datetime | None = None) -> int:
        """Count unanswered recommendations whose 48-hour owner-response window elapsed."""
        reference = now or utcnow()
        cutoff = reference - timedelta(hours=48)
        with self.session() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(RecommendationRow)
                    .where(
                        RecommendationRow.status == "new",
                        RecommendationRow.created_at <= cutoff,
                    )
                )
                or 0
            )

    def expire_stale_recommendations(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Idempotently mark unanswered recommendations older than 48 hours as ignored."""
        reference = now or utcnow()
        cutoff = reference - timedelta(hours=48)
        with self.session() as session:
            eligible = int(
                session.scalar(
                    select(func.count())
                    .select_from(RecommendationRow)
                    .where(
                        RecommendationRow.status == "new",
                        RecommendationRow.created_at <= cutoff,
                    )
                )
                or 0
            )
            if eligible:
                session.query(RecommendationRow).filter(
                    RecommendationRow.status == "new",
                    RecommendationRow.created_at <= cutoff,
                ).update(
                    {RecommendationRow.status: "ignored", RecommendationRow.updated_at: reference},
                    synchronize_session=False,
                )

        return {
            "cutoff": cutoff.isoformat(),
            "eligible": eligible,
            "ignored": eligible,
            "status_counts": self.recommendation_status_counts(),
        }

    def set_recommendation_feedback(
        self,
        recommendation_id: str,
        status: str,
        owner_note: str | None = None,
    ) -> dict[str, Any]:
        """Idempotently store owner feedback using the existing lifecycle tables."""
        if status not in {"applied", "rejected"}:
            raise ValueError("status must be applied or rejected")
        note = (owner_note or "").strip()
        with self.session() as session:
            row = session.get(RecommendationRow, recommendation_id)
            if not row:
                raise KeyError(recommendation_id)

            current_note = row.rejection_reason or ""
            latest_intervention: InterventionRow | None = None
            if row.status == "applied":
                latest_intervention = self._latest_intervention(session, recommendation_id)
                current_note = latest_intervention.note if latest_intervention else ""
            if row.status == status and current_note == note:
                return self._recommendation_view(session, row)

            row.status = status
            row.updated_at = utcnow()
            if status == "applied":
                row.rejection_reason = None
                intervention = InterventionRow(
                    id=f"intervention:{uuid.uuid4()}",
                    recommendation_id=recommendation_id,
                    note=note,
                )
                session.add(intervention)
                session.flush()
            else:
                row.rejection_reason = note
            session.flush()
            return self._recommendation_view(session, row)

    def mark_applied(self, recommendation_id: str, note: str) -> str:
        feedback = self.set_recommendation_feedback(recommendation_id, "applied", note)
        intervention_id = feedback.get("intervention_id")
        if not isinstance(intervention_id, str):
            raise RuntimeError(f"Applied recommendation {recommendation_id} has no intervention")
        return intervention_id

    def reject(self, recommendation_id: str, reason: str) -> None:
        self.set_recommendation_feedback(recommendation_id, "rejected", reason)

    def flush_log_outbox(self) -> list[str]:
        delivered: list[str] = []
        with self.session() as session:
            rows = session.scalars(
                select(NotificationOutboxRow).where(
                    NotificationOutboxRow.channel == "log",
                    NotificationOutboxRow.status == "pending",
                )
            ).all()
            for row in rows:
                delivered.append(row.payload)
                row.status = "delivered"
                row.attempts += 1
        return delivered

    def save_llm_call(self, **values: Any) -> None:
        with self.session() as session:
            session.add(LlmCallRow(**values))

    def token_usage_this_month(self) -> int:
        now = utcnow()
        month_start = datetime(now.year, now.month, 1, tzinfo=UTC)
        with self.session() as session:
            value = session.scalar(
                select(func.sum(LlmCallRow.input_tokens + LlmCallRow.output_tokens)).where(
                    LlmCallRow.created_at >= month_start
                )
            )
            return int(value or 0)

    def status(self) -> dict[str, Any]:
        latest = self.latest_sample_time()
        earliest = self.earliest_sample_time()
        with self.session() as session:
            return {
                "db_path": str(self.path),
                "db_size_bytes": self.path.stat().st_size if self.path.exists() else 0,
                "schema_revision": self.current_revision(),
                "integrity": self.integrity_check(),
                "devices": int(session.scalar(select(func.count()).select_from(DeviceRow)) or 0),
                "series": int(session.scalar(select(func.count()).select_from(TelemetrySeriesRow)) or 0),
                "samples": int(session.scalar(select(func.count()).select_from(TelemetrySampleRow)) or 0),
                "source_events": int(session.scalar(select(func.count()).select_from(SourceEventRow)) or 0),
                "reports": int(session.scalar(select(func.count()).select_from(ReportRow)) or 0),
                "pending_notifications": int(
                    session.scalar(
                        select(func.count())
                        .select_from(NotificationOutboxRow)
                        .where(NotificationOutboxRow.status == "pending")
                    )
                    or 0
                ),
                "earliest_sample": earliest.isoformat() if earliest else None,
                "latest_sample": latest.isoformat() if latest else None,
                "monthly_ai_tokens": self.token_usage_this_month(),
            }

    def backup(self, destination_dir: Path) -> Path:
        destination_dir.mkdir(parents=True, exist_ok=True)
        timestamp = utcnow().strftime("%Y%m%dT%H%M%S%fZ")
        destination = destination_dir / f"zont-analyzer-{timestamp}.sqlite3"
        source_connection = sqlite3.connect(self.path)
        try:
            target_connection = sqlite3.connect(destination)
            try:
                source_connection.backup(target_connection)
            finally:
                target_connection.close()
        finally:
            source_connection.close()
        if not destination.exists() or destination.stat().st_size == 0:
            raise RuntimeError("SQLite backup is empty")
        check = sqlite3.connect(destination).execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            shutil.move(destination, destination.with_suffix(".corrupt"))
            raise RuntimeError(f"Backup integrity check failed: {check}")
        return destination
