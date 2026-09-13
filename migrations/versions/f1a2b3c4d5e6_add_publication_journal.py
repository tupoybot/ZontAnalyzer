"""Journal durable inputs that require incremental publication."""
# Trigger predicates are SQL expressions; keeping each expression legible is
# preferable to splitting them into opaque fragments.
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "d92a10b4c601"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _trigger(name: str, table: str, event: str, body: str, when: str | None = None) -> None:
    condition = f" WHEN {when}" if when else ""
    op.execute(sa.text(f"CREATE TRIGGER {name} AFTER {event} ON {table}{condition} BEGIN {body}; END"))


def _mark(scope: str, expression: str) -> str:
    return (
        "INSERT INTO publication_changes(scope, identifier) VALUES "
        f"('{scope}', {expression}) ON CONFLICT(scope, identifier) DO UPDATE SET revision=excluded.revision"
    )


def _mark_select(scope: str, expression: str) -> str:
    return (
        "INSERT INTO publication_changes(scope, identifier) SELECT "
        f"'{scope}', {expression} WHERE {expression} IS NOT NULL "
        "ON CONFLICT(scope, identifier) DO UPDATE SET revision=excluded.revision"
    )


def upgrade() -> None:
    op.create_table(
        "publication_changes",
        sa.Column("revision", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("identifier", sa.String(), nullable=False),
        sa.UniqueConstraint("scope", "identifier"),
        sqlite_autoincrement=True,
    )

    _trigger("publication_reports_insert", "reports", "INSERT", _mark("report", "NEW.id"))
    _trigger("publication_reports_update", "reports", "UPDATE", _mark("report", "NEW.id"),
             "OLD.kind IS NOT NEW.kind OR OLD.period_start IS NOT NEW.period_start OR "
             "OLD.period_end IS NOT NEW.period_end OR OLD.canonical_json IS NOT NEW.canonical_json OR "
             "OLD.generated_at IS NOT NEW.generated_at OR OLD.algorithm_version IS NOT NEW.algorithm_version")
    _trigger("publication_reports_delete", "reports", "DELETE", _mark("report", "OLD.id"))

    _trigger("publication_recommendations_insert", "recommendations", "INSERT", _mark("render", "NEW.report_id"))
    _trigger("publication_recommendations_update", "recommendations", "UPDATE", _mark("render", "NEW.report_id"),
             "OLD.report_id IS NOT NEW.report_id OR OLD.category IS NOT NEW.category OR OLD.priority IS NOT NEW.priority OR "
             "OLD.status IS NOT NEW.status OR OLD.payload_json IS NOT NEW.payload_json OR OLD.rejection_reason IS NOT NEW.rejection_reason")
    _trigger("publication_recommendations_delete", "recommendations", "DELETE", _mark("render", "OLD.report_id"))

    _trigger("publication_interventions_insert", "interventions", "INSERT",
             _mark_select("render", "(SELECT report_id FROM recommendations WHERE id=NEW.recommendation_id)" ) + ";" + _mark("global", "'gas'"))
    _trigger("publication_interventions_update", "interventions", "UPDATE",
             _mark_select("render", "(SELECT report_id FROM recommendations WHERE id=NEW.recommendation_id)") + ";" + _mark("global", "'gas'"),
             "OLD.recommendation_id IS NOT NEW.recommendation_id OR OLD.applied_at IS NOT NEW.applied_at OR OLD.note IS NOT NEW.note")
    _trigger("publication_interventions_delete", "interventions", "DELETE",
             _mark_select("render", "(SELECT report_id FROM recommendations WHERE id=OLD.recommendation_id)") + ";" + _mark("global", "'gas'"))
    _trigger("publication_experiments_insert", "intervention_experiments", "INSERT",
             _mark_select("render", "(SELECT r.report_id FROM interventions i JOIN recommendations r ON r.id=i.recommendation_id WHERE i.id=NEW.intervention_id)") + ";" + _mark("global", "'gas'"))
    _trigger("publication_experiments_update", "intervention_experiments", "UPDATE",
             _mark_select("render", "(SELECT r.report_id FROM interventions i JOIN recommendations r ON r.id=i.recommendation_id WHERE i.id=NEW.intervention_id)") + ";" + _mark("global", "'gas'"),
             "OLD.intervention_id IS NOT NEW.intervention_id OR OLD.category IS NOT NEW.category OR OLD.parameter IS NOT NEW.parameter OR "
             "OLD.before_json IS NOT NEW.before_json OR OLD.after_json IS NOT NEW.after_json OR OLD.performed_at IS NOT NEW.performed_at OR "
             "OLD.snapshot_json IS NOT NEW.snapshot_json OR OLD.snapshot_fingerprint IS NOT NEW.snapshot_fingerprint OR OLD.snapshot_source IS NOT NEW.snapshot_source OR OLD.snapshot_captured_at IS NOT NEW.snapshot_captured_at OR OLD.historical_context IS NOT NEW.historical_context")
    _trigger("publication_experiments_delete", "intervention_experiments", "DELETE",
             _mark_select("render", "(SELECT r.report_id FROM interventions i JOIN recommendations r ON r.id=i.recommendation_id WHERE i.id=OLD.intervention_id)") + ";" + _mark("global", "'gas'"))

    gas_conditions = {
        "gas_readings": "OLD.device_id IS NOT NEW.device_id OR OLD.report_id IS NOT NEW.report_id OR OLD.reading_day IS NOT NEW.reading_day OR OLD.meter_segment IS NOT NEW.meter_segment OR OLD.value_m3 IS NOT NEW.value_m3",
        "gas_meter_boundaries": "OLD.device_id IS NOT NEW.device_id OR OLD.report_id IS NOT NEW.report_id OR OLD.boundary_day IS NOT NEW.boundary_day",
    }
    for table in ("gas_readings", "gas_meter_boundaries"):
        _trigger(f"publication_{table}_insert", table, "INSERT", _mark("global", "'gas'"))
        _trigger(f"publication_{table}_update", table, "UPDATE", _mark("global", "'gas'"), gas_conditions[table])
        _trigger(f"publication_{table}_delete", table, "DELETE", _mark("global", "'gas'"))
    _trigger("publication_profiles_insert", "owner_profile_revisions", "INSERT", _mark("global", "'gas'"))
    _trigger("publication_profiles_update", "owner_profile_revisions", "UPDATE", _mark("global", "'gas'"),
             "OLD.device_id IS NOT NEW.device_id OR OLD.field IS NOT NEW.field OR OLD.value_json IS NOT NEW.value_json OR "
             "OLD.source IS NOT NEW.source OR OLD.provenance IS NOT NEW.provenance OR OLD.effective_from IS NOT NEW.effective_from OR OLD.is_reset IS NOT NEW.is_reset")
    _trigger("publication_profiles_delete", "owner_profile_revisions", "DELETE", _mark("global", "'gas'"))
    meaningful = {
        "gas_tariffs": "OLD.scope IS NOT NEW.scope OR OLD.effective_month IS NOT NEW.effective_month OR OLD.effective_from IS NOT NEW.effective_from OR OLD.price IS NOT NEW.price OR OLD.currency IS NOT NEW.currency",
        "ai_settings_revisions": "OLD.values_json IS NOT NEW.values_json OR OLD.before_json IS NOT NEW.before_json",
        "model_review_state": "OLD.scope IS NOT NEW.scope OR OLD.last_success_at IS NOT NEW.last_success_at OR OLD.next_due_at IS NOT NEW.next_due_at OR OLD.last_attempt_at IS NOT NEW.last_attempt_at OR OLD.attempts IS NOT NEW.attempts OR OLD.lease_token IS NOT NEW.lease_token OR OLD.lease_until IS NOT NEW.lease_until OR OLD.last_error IS NOT NEW.last_error",
        "model_review_runs": "OLD.scope IS NOT NEW.scope OR OLD.started_at IS NOT NEW.started_at OR OLD.finished_at IS NOT NEW.finished_at OR OLD.trigger IS NOT NEW.trigger OR OLD.status IS NOT NEW.status OR OLD.settings_version IS NOT NEW.settings_version OR OLD.settings_json IS NOT NEW.settings_json OR OLD.sources_json IS NOT NEW.sources_json OR OLD.catalog_json IS NOT NEW.catalog_json OR OLD.result_json IS NOT NEW.result_json OR OLD.error IS NOT NEW.error",
        "model_review_proposals": "OLD.run_id IS NOT NEW.run_id OR OLD.status IS NOT NEW.status OR OLD.settings_version IS NOT NEW.settings_version OR OLD.profile IS NOT NEW.profile OR OLD.current_model IS NOT NEW.current_model OR OLD.candidate_model IS NOT NEW.candidate_model OR OLD.recommendation_json IS NOT NEW.recommendation_json OR OLD.decided_at IS NOT NEW.decided_at OR OLD.decision_note IS NOT NEW.decision_note OR OLD.version IS NOT NEW.version",
    }
    for table in meaningful:
        if table == "gas_tariffs":
            for event in ("INSERT", "UPDATE", "DELETE"):
                row = "OLD" if event == "DELETE" else "NEW"
                body = _mark("tariff", f"{row}.effective_from") + ";" + _mark("global", "'render'")
                if event == "UPDATE":
                    body += ";" + _mark("tariff", "OLD.effective_from")
                _trigger(f"publication_{table}_{event.lower()}", table, event, body,
                         meaningful[table] if event == "UPDATE" else None)
            continue
        scope = "cost" if table == "gas_tariffs" else "render"
        _trigger(f"publication_{table}_insert", table, "INSERT", _mark("global", f"'{scope}'"))
        _trigger(f"publication_{table}_update", table, "UPDATE", _mark("global", f"'{scope}'"), meaningful[table])
        _trigger(f"publication_{table}_delete", table, "DELETE", _mark("global", f"'{scope}'"))

    for event in ("INSERT", "UPDATE", "DELETE"):
        _trigger(f"publication_series_{event.lower()}", "telemetry_series", event, _mark("global", "'gas'"),
                 None if event != "UPDATE" else "OLD.device_id IS NOT NEW.device_id OR OLD.source_type IS NOT NEW.source_type OR OLD.entity_id IS NOT NEW.entity_id OR OLD.metric_key IS NOT NEW.metric_key OR OLD.unit IS NOT NEW.unit OR OLD.display_name IS NOT NEW.display_name OR OLD.role IS NOT NEW.role OR OLD.confidence IS NOT NEW.confidence OR OLD.provenance IS NOT NEW.provenance OR OLD.origin IS NOT NEW.origin")
        _trigger(f"publication_devices_{event.lower()}", "devices", event, _mark("global", "'gas'"),
                 None if event != "UPDATE" else "OLD.name IS NOT NEW.name OR OLD.model IS NOT NEW.model")


def downgrade() -> None:
    for name in [row[0] for row in op.get_bind().execute(sa.text("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'publication_%'"))]:
        op.execute(sa.text(f"DROP TRIGGER {name}"))
    op.drop_table("publication_changes")
