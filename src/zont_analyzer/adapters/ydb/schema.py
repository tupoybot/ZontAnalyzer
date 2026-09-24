"""Schema v2. No source retention TTL is applied to the application's history."""

TABLES = {
    "migration_records": (
        "source_table Utf8 NOT NULL, source_key Utf8 NOT NULL, target_table Utf8, target_key Utf8, "
        "checksum Utf8, payload Utf8, PRIMARY KEY(source_table,source_key)"
    ),
    "app_meta": "key Utf8 NOT NULL, value Utf8, PRIMARY KEY(key)",
    "ai_budget_months": (
        "month Utf8 NOT NULL, reserved_tokens Int64, charged_tokens Int64, PRIMARY KEY(month)"
    ),
    "data_gaps": "id Int64 NOT NULL, payload Utf8, PRIMARY KEY(id)",
    "analysis_periods": "id Utf8 NOT NULL, payload Utf8, PRIMARY KEY(id)",
    "metric_values": "id Utf8 NOT NULL, period_id Utf8, payload Utf8, PRIMARY KEY(id)",
    "detected_events": "id Utf8 NOT NULL, period_id Utf8, payload Utf8, PRIMARY KEY(id)",
    "interventions": (
        "id Utf8 NOT NULL, recommendation_id Utf8, applied_at Int64, payload Utf8, "
        "INDEX by_applied_at GLOBAL SYNC ON (applied_at), PRIMARY KEY(id)"
    ),
    "intervention_experiments": "id Utf8 NOT NULL, intervention_id Utf8, payload Utf8, PRIMARY KEY(id)",
    "metadata": "name Utf8 NOT NULL, value Utf8, PRIMARY KEY (name)",
    "sequences": "name Utf8 NOT NULL, value Int64, PRIMARY KEY (name)",
    "devices": "id Utf8 NOT NULL, payload Utf8, PRIMARY KEY (id)",
    "entities": "device_id Utf8 NOT NULL, id Utf8 NOT NULL, payload Utf8, PRIMARY KEY (device_id, id)",
    "config_snapshots": (
        "device_id Utf8 NOT NULL, content_hash Utf8 NOT NULL, id Int64, payload Utf8, captured_at Int64, "
        "PRIMARY KEY (device_id, content_hash)"
    ),
    "telemetry_series": (
        "device_id Utf8 NOT NULL, source_type Utf8 NOT NULL, entity_id Utf8 NOT NULL, metric_key Utf8 NOT NULL, "
        "id Int64, payload Utf8, INDEX by_id GLOBAL SYNC ON (id), "
        "PRIMARY KEY (device_id, source_type, entity_id, metric_key)"
    ),
    "telemetry_samples": (
        "series_id Int64 NOT NULL, timestamp_utc Int64 NOT NULL, value_num Double, value_text Utf8, "
        "quality Utf8, ingested_at Int64, PRIMARY KEY (series_id, timestamp_utc)"
    ),
    "coverage": (
        "device_id Utf8 NOT NULL, data_type Utf8 NOT NULL, started_at Int64 NOT NULL, ended_at Int64 NOT NULL, "
        "state Utf8, checked_at Int64, PRIMARY KEY (device_id, data_type, started_at, ended_at)"
    ),
    "ingestion_cursors": (
        "device_id Utf8 NOT NULL, data_type Utf8 NOT NULL, timestamp_utc Int64, "
        "PRIMARY KEY (device_id, data_type)"
    ),
    "source_events": (
        "device_id Utf8 NOT NULL, timestamp_utc Int64 NOT NULL, id Utf8 NOT NULL, payload Utf8, "
        "INDEX by_id GLOBAL SYNC ON (id), "
        "PRIMARY KEY (device_id, timestamp_utc, id)"
    ),
    "revisions": "scope Utf8 NOT NULL, revision Int64, PRIMARY KEY (scope)",
    "reports": (
        "kind Utf8 NOT NULL, period_start Int64 NOT NULL, period_end Int64 NOT NULL, "
        "algorithm_version Utf8 NOT NULL, id Utf8, payload Utf8, revision Int64, "
        "INDEX by_id GLOBAL SYNC ON (id), PRIMARY KEY (kind, period_start, period_end, algorithm_version)"
    ),
    "jobs": (
        "job_key Utf8 NOT NULL, owner Utf8, attempt Int64, lease_until Int64, state Utf8, checkpoint Utf8, "
        "PRIMARY KEY (job_key)"
    ),
    "llm_calls": (
        "call_key Utf8 NOT NULL, job_key Utf8, state Utf8, payload Utf8, updated_at Int64, "
        "created_at Int64, sent_at Int64, PRIMARY KEY (call_key)"
    ),
    "publication_changes": (
        "scope Utf8 NOT NULL, identifier Utf8 NOT NULL, revision Int64, payload Utf8, "
        "INDEX by_revision GLOBAL SYNC ON (revision), "
        "PRIMARY KEY (scope, identifier)"
    ),
    "publication_items": (
        "href Utf8 NOT NULL, report_id Utf8, kind Utf8, period_start Int64, period_end Int64, "
        "generated_at Int64, digest Utf8, lo Double, hi Double, comparisons Bool, dirty Int64, "
        "queued_at Int64, entry Utf8, json_stamp Utf8, html_stamp Utf8, "
        "INDEX by_queue GLOBAL SYNC ON (dirty,queued_at), "
        "INDEX by_kind_start GLOBAL SYNC ON (kind,period_start), PRIMARY KEY(href)"
    ),
    "owner_profile_revisions": (
        "device_id Utf8 NOT NULL, revision Int64 NOT NULL, field Utf8 NOT NULL, effective_at Int64, payload Utf8, "
        "PRIMARY KEY (device_id, field, revision)"
    ),
    "gas_readings": (
        "device_id Utf8 NOT NULL, reading_day Utf8 NOT NULL, payload Utf8, PRIMARY KEY (device_id,reading_day)"
    ),
    "gas_reading_audit": (
        "id Int64 NOT NULL, device_id Utf8, reading_day Utf8, at Int64, payload Utf8, PRIMARY KEY (id)"
    ),
    "gas_tariffs": (
        "scope Utf8 NOT NULL, effective_month Utf8 NOT NULL, payload Utf8, PRIMARY KEY (scope,effective_month)"
    ),
    "gas_tariff_audit": "id Int64 NOT NULL, scope Utf8, effective_month Utf8, at Int64, payload Utf8, PRIMARY KEY (id)",
    "ai_settings_revisions": (
        "scope Utf8 NOT NULL, version Int64 NOT NULL, effective_at Int64, payload Utf8, PRIMARY KEY (scope,version)"
    ),
    "model_review_proposals": "id Utf8 NOT NULL, payload Utf8, PRIMARY KEY (id)",
    "model_review_state": "scope Utf8 NOT NULL, payload Utf8, version Int64, PRIMARY KEY (scope)",
    "gas_meter_boundaries": (
        "device_id Utf8 NOT NULL, boundary_day Utf8 NOT NULL, payload Utf8, PRIMARY KEY (device_id,boundary_day)"
    ),
    "gas_meter_boundary_audit": (
        "id Int64 NOT NULL, device_id Utf8, boundary_day Utf8, at Int64, payload Utf8, PRIMARY KEY (id)"
    ),
    "recommendations": (
        "id Utf8 NOT NULL, report_id Utf8, payload Utf8, status Utf8, note Utf8, experiment Utf8, "
        "created_at Int64, updated_at Int64, "
        "PRIMARY KEY (id)"
    ),
    "recommendation_audit": "id Int64 NOT NULL, recommendation_id Utf8, at Int64, payload Utf8, PRIMARY KEY (id)",
    "notification_outbox": (
        "id Utf8 NOT NULL, report_id Utf8, channel Utf8, payload Utf8, state Utf8, attempts Int64, PRIMARY KEY (id)"
    ),
    "ai_response_cache": (
        "fingerprint Utf8 NOT NULL, payload Utf8, provenance Utf8, settings_version Utf8, model Utf8, "
        "created_at Int64, PRIMARY KEY (fingerprint)"
    ),
    "model_review_runs": (
        "id Utf8 NOT NULL, scope Utf8, started_at Int64, status Utf8, payload Utf8, PRIMARY KEY (id)"
    ),
}
