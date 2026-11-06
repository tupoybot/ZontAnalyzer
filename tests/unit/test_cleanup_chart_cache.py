"""Offline planning and transactional byte fences for legacy chart cleanup."""
from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tools import cleanup_chart_cache
from tools.cleanup_chart_cache import apply_chart_cleanup, main, plan_chart_cleanup
from zont_analyzer.domain import QualityResult, Report
from zont_analyzer.reports.chart_data import _cache_key, _encode_chart_cache, _legacy_cache_key


def _fixture():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    report = Report(id="fixture", kind="daily", period_start=start, period_end=start + timedelta(days=1),
                    generated_at=start + timedelta(days=1), summary="saved report",
                    quality=QualityResult(score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
                                          implausible_jumps=0, sample_count=1))
    target, digest = _cache_key(report)
    current = _legacy_cache_key(report)
    obsolete = current.rsplit(":", 1)[0] + ":" + "0" * 64
    encoded = _encode_chart_cache(digest, {"series": {"retained": {"points": [1, 2]}}})
    reports = [{"id": report.id, "payload": json.dumps({"report": report.model_dump(mode="json")})}]
    meta = [{"key": current, "value": encoded}, {"key": obsolete, "value": "old packet"},
            {"key": "gas-model:fixture", "value": "keep"},
            {"key": "chart-data:v2:unknown", "value": "keep"}]
    return reports, meta, target, current, obsolete, encoded


class _Database:
    def __init__(self, reports, metadata):
        self.reports = {row["id"]: row["payload"] for row in reports}
        self.meta = {row["key"]: row["value"] for row in metadata}
        self.fail_delete = False

    def transaction(self, callback):
        candidate = copy.deepcopy(self)
        result = callback(candidate)
        self.meta = candidate.meta
        return result

    def execute(self, query, parameters):
        if "SELECT payload FROM reports" in query:
            value = self.reports.get(parameters["$id"])
            rows = [SimpleNamespace(payload=value)] if value is not None else []
        elif "SELECT value FROM app_meta" in query:
            value = self.meta.get(parameters["$key"])
            rows = [SimpleNamespace(value=value)] if value is not None else []
        elif "UPSERT INTO app_meta" in query:
            self.meta[parameters["$key"]] = parameters["$value"]
            rows = []
        elif "DELETE FROM app_meta" in query:
            if self.fail_delete:
                raise RuntimeError("injected transaction failure")
            self.meta.pop(parameters["$key"], None)
            rows = []
        else:
            raise AssertionError("unexpected query")
        return [SimpleNamespace(rows=rows)]


def test_migration_preserves_packet_bytes_unknown_keys_and_restart():
    reports, meta, target, current, obsolete, encoded = _fixture()
    original = copy.deepcopy((reports, meta))
    plan = plan_chart_cleanup(reports, meta)
    assert (reports, meta) == original
    assert plan["summary"]["legacy_rows_to_delete"] == 2
    db = _Database(reports, meta)
    result = apply_chart_cleanup(db, plan)
    assert result["deleted_rows"] == 2
    assert result["upserted_rows"] == 1
    assert db.meta == {target: encoded, "gas-model:fixture": "keep", "chart-data:v2:unknown": "keep"}
    assert db.reports == {row["id"]: row["payload"] for row in reports}
    repeated = apply_chart_cleanup(db, plan)
    assert repeated["already_complete_reports"] == 1
    assert repeated["deleted_rows"] == repeated["upserted_rows"] == 0


@pytest.mark.parametrize("mutation", ["report", "source", "target", "missing_source"])
def test_concurrent_mutation_skips_whole_report_without_any_write(mutation):
    reports, meta, target, current, obsolete, encoded = _fixture()
    plan = plan_chart_cleanup(reports, meta)
    db = _Database(reports, meta)
    if mutation == "report":
        db.reports["fixture"] += " "
    elif mutation == "source":
        db.meta[obsolete] = "new source bytes"
    elif mutation == "target":
        db.meta[target] = "new target bytes"
    else:
        del db.meta[current]
    before = copy.deepcopy(db.meta)
    assert apply_chart_cleanup(db, plan)["conflicted_reports"] == 1
    assert db.meta == before


def test_valid_existing_v3_wins_and_missing_legacy_is_safe_after_verified_target():
    reports, meta, target, current, obsolete, encoded = _fixture()
    existing = json.dumps(json.loads(encoded), indent=2)
    meta.append({"key": target, "value": existing})
    plan = plan_chart_cleanup(reports, meta)
    db = _Database(reports, meta)
    del db.meta[current]
    result = apply_chart_cleanup(db, plan)
    assert result["deleted_rows"] == 1
    assert result["upserted_rows"] == 0
    assert db.meta[target] == existing


def test_missing_or_corrupt_current_packet_leaves_report_completely_untouched():
    reports, meta, target, current, obsolete, encoded = _fixture()
    for matching in (None, "corrupt", encoded.replace('"report_digest":', '"wrong_digest":')):
        metadata = [row for row in meta if row["key"] != current]
        if matching is not None:
            metadata.append({"key": current, "value": matching})
        plan = plan_chart_cleanup(reports, metadata)
        assert plan["report_plans"] == []
        assert plan["summary"]["reports_without_current_packet"] == 1


def test_failed_transaction_does_not_leave_migrated_or_deleted_bytes():
    reports, meta, *_ = _fixture()
    plan = plan_chart_cleanup(reports, meta)
    db = _Database(reports, meta)
    db.fail_delete = True
    before = copy.deepcopy(db.meta)
    with pytest.raises(RuntimeError, match="injected"):
        apply_chart_cleanup(db, plan)
    assert db.meta == before


@pytest.mark.parametrize("tamper", ["foreign_key", "target", "packet", "duplicate_report"])
def test_tampered_plan_is_rejected_before_transactions(tamper):
    reports, meta, target, current, obsolete, encoded = _fixture()
    plan = plan_chart_cleanup(reports, meta)
    action = plan["report_plans"][0]
    if tamper == "foreign_key":
        action["sources"]["gas-model:fixture"] = "keep"
    elif tamper == "target":
        action["target_key"] = "gas-model:fixture"
    elif tamper == "packet":
        action["after_target"] = encoded.replace('[1,2]', '[9,9]')
    else:
        plan["report_plans"].append(copy.deepcopy(action))
    db = _Database(reports, meta)
    before = copy.deepcopy(db.meta)
    with pytest.raises(ValueError):
        apply_chart_cleanup(db, plan)
    assert db.meta == before


def test_offline_cli_creates_private_plan_and_prints_only_aggregate_summary(tmp_path, monkeypatch, capsys):
    reports, meta, *_ = _fixture()
    report_file, meta_file, output = (tmp_path / name for name in ("reports.jsonl", "meta.jsonl", "plan.json"))
    report_file.write_text("\n".join(json.dumps(row) for row in reports))
    meta_file.write_text("\n".join(json.dumps(row) for row in meta))
    monkeypatch.setattr("sys.argv", ["cleanup", "plan", str(report_file), str(meta_file), str(output)])
    main()
    printed = capsys.readouterr().out
    assert json.loads(printed)["legacy_rows_to_delete"] == 2
    assert "fixture" not in printed and "chart-data" not in printed
    assert output.stat().st_mode & 0o777 == 0o600
    saved = output.read_bytes()
    with pytest.raises(FileExistsError):
        main()
    assert output.read_bytes() == saved


def test_aggregate_summary_counts_utf8_payload_bytes_and_unmatched_legacy_rows():
    reports, meta, target, current, obsolete, encoded = _fixture()
    extra = json.loads(reports[0]["payload"])
    extra["report"]["id"] = "missing-packet"
    report = Report.model_validate(extra["report"])
    reports.append({"id": report.id, "payload": json.dumps(extra)})
    meta.extend([
        {"key": _legacy_cache_key(report), "value": "corrupt current packet"},
        {"key": "chart-data:v2:" + "f" * 64 + ":" + "0" * 64, "value": "orphan"},
    ])
    meta[1]["value"] = "старые данные"
    summary = plan_chart_cleanup(reports, meta)["summary"]
    source_bytes = len(encoded.encode()) + len("старые данные".encode())
    assert summary["source_payload_bytes"] == source_bytes
    assert summary["existing_target_payload_bytes"] == 0
    assert summary["retained_target_payload_bytes"] == len(encoded.encode())
    assert summary["net_payload_bytes_removed"] == len("старые данные".encode())
    assert summary["unmatched_legacy_rows"] == 2
    assert summary["orphan_legacy_rows"] == summary["no_current_packet_legacy_rows"] == 1


def test_source_key_limit_accepts_128_and_rejects_129_before_database_calls():
    reports, meta, target, current, obsolete, encoded = _fixture()
    prefix = current.rsplit(":", 1)[0]
    meta.extend({"key": f"{prefix}:{number:064x}", "value": "old"} for number in range(1, 127))
    plan = plan_chart_cleanup(reports, meta)
    assert len(plan["report_plans"][0]["sources"]) == cleanup_chart_cache.MAX_SOURCE_KEYS == 128
    meta.append({"key": f"{prefix}:{127:064x}", "value": "old"})
    with pytest.raises(ValueError, match="source-key limit"):
        plan_chart_cleanup(reports, meta)
    # Even an invalid later action must prevent applying an earlier valid one.
    oversized = copy.deepcopy(plan["report_plans"][0])
    oversized["sources"][f"{prefix}:{127:064x}"] = "old"
    plan["report_plans"].append(oversized)

    class NoDatabase:
        def transaction(self, _callback):
            raise AssertionError("preflight must finish before any database call")

    with pytest.raises(ValueError, match="source-key limit"):
        apply_chart_cleanup(NoDatabase(), plan)


def test_transaction_byte_limit_is_utf8_inclusive_and_checked_before_database_calls(monkeypatch):
    reports, meta, target, current, obsolete, encoded = _fixture()
    meta[1]["value"] = "Ж" * 10
    plan = plan_chart_cleanup(reports, meta)
    action = plan["report_plans"][0]
    size = sum(len(value.encode()) for value in action["sources"].values())
    size += len(action["after_target"].encode()) + len(action["expected_report_payload"].encode())
    assert cleanup_chart_cache.MAX_ACTION_BYTES == 8 * 1024 * 1024
    monkeypatch.setattr(cleanup_chart_cache, "MAX_ACTION_BYTES", size)
    assert apply_chart_cleanup(_Database(reports, meta), plan)["applied_reports"] == 1
    monkeypatch.setattr(cleanup_chart_cache, "MAX_ACTION_BYTES", size - 1)

    class NoDatabase:
        def transaction(self, _callback):
            raise AssertionError("preflight must finish before any database call")

    with pytest.raises(ValueError, match="payload-byte limit"):
        apply_chart_cleanup(NoDatabase(), plan)
    with pytest.raises(ValueError, match="payload-byte limit"):
        plan_chart_cleanup(reports, meta)
