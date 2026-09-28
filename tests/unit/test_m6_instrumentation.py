"""Domain outcomes must survive HTTP success and retries without exposing payloads."""
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import httpx
import pytest

from zont_analyzer.adapters.openai import provider
from zont_analyzer.adapters.ydb import database
from zont_analyzer.application import collection, publication
from zont_analyzer.cloud import egress
from zont_analyzer.config import AppConfig


def test_caught_collection_failure_is_not_a_successful_sync(monkeypatch):
    observe = Mock()
    monkeypatch.setattr(collection, "observe", observe)
    db, client = Mock(), Mock()
    end = datetime(2026, 1, 2, tzinfo=UTC)
    start = end - timedelta(minutes=30)
    db.list_devices.return_value = [{"id": "fixture"}]
    db.get_app_meta.return_value = None
    db.telemetry.missing_intervals.return_value = [(start.timestamp(), end.timestamp(), "missing")]
    config = AppConfig()
    config.zont.history_data_types = []
    client.load_events.side_effect = RuntimeError("private upstream body")
    result = collection.CollectionService(db, client, config).ensure_period(start, end, now=end)
    assert result["failed_windows"] == 1
    observe.assert_any_call("zont_sync_success", 0.0)
    assert not any(call.args[0] == "zont_telemetry_timestamp_seconds" for call in observe.call_args_list)
    assert "private upstream body" not in str(observe.call_args_list)


def test_ydb_retried_callback_counts_only_terminal_failure(monkeypatch):
    observe = Mock()
    monkeypatch.setattr(database, "observe", observe)
    db = object.__new__(database.YdbDatabase)
    db.prefix = ""
    session = MagicMock()
    raw = session.transaction.return_value.__enter__.return_value
    attempts = 0

    def callback(_):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("private query data")

    def retries(run):
        for _ in range(2):
            with suppress(RuntimeError):
                run(session)
        return run(session)

    db.pool = SimpleNamespace(retry_operation_sync=retries)
    with pytest.raises(RuntimeError):
        db.transaction(callback)
    assert attempts == 3
    raw.commit.assert_not_called()
    observe.assert_called_once_with("zont_ydb_errors_total")


def test_openai_dispatch_failure_counted_once_and_cache_never_dispatches(monkeypatch):
    observe = Mock()
    monkeypatch.setattr(provider, "observe", observe)
    analyst = object.__new__(provider.OpenAIAnalyst)
    analyst.config = AppConfig()
    analyst.dispatch_deadline = None
    analyst.ledger = Mock()
    analyst.ledger.cached.return_value = None
    analyst.ledger.reserve.return_value = None
    analyst.ledger.mark_sent.return_value = True
    analyst.client = Mock()
    analyst.client.responses.parse.side_effect = RuntimeError("private provider body")
    with pytest.raises(provider.AIRequestPending):
        analyst.analyze({})
    observe.assert_any_call("zont_openai_calls_total")
    observe.assert_any_call("zont_openai_failures_total")
    assert analyst.client.responses.parse.call_count == 1
    observe.reset_mock()
    analyst.ledger.cached.return_value = {"status": "success", "result": {"summary": "cached"}}
    assert analyst.analyze({}).summary == "cached"
    assert analyst.client.responses.parse.call_count == 1
    observe.assert_not_called()


def test_publication_failure_keeps_exception_and_records_counter(monkeypatch):
    observe = Mock()
    monkeypatch.setattr(publication, "observe", observe)
    monkeypatch.setattr(publication, "_publish_reports", Mock(side_effect=OSError("private path")))
    with pytest.raises(OSError):
        publication.publish_reports(Mock())
    observe.assert_called_once_with("zont_publication_failures_total")


def test_proxy_transport_failure_and_upstream_status_are_distinct(monkeypatch):
    observe = Mock()
    monkeypatch.setattr(egress, "observe", observe)
    direct, proxied = Mock(), Mock()
    proxied.handle_request.side_effect = httpx.ProxyError("private tunnel credentials")
    transport = egress.ReportTransport(direct=direct, proxied=proxied)
    request = httpx.Request("POST", "https://api.openai.com/v1/responses", content=b"{}")
    with pytest.raises(httpx.ProxyError):
        transport.handle_request(request)
    direct.handle_request.assert_not_called()
    observe.assert_any_call("zont_proxy_failures_total", destination="openai")
    assert not any(call.args[0] == "zont_upstream_failures_total" for call in observe.call_args_list)
    observe.reset_mock()
    proxied.handle_request.side_effect = None
    proxied.handle_request.return_value = httpx.Response(429, content=b"busy")
    assert transport.handle_request(request).status_code == 429
    observe.assert_any_call("zont_upstream_failures_total", destination="openai")
    observe.assert_any_call("zont_egress_bytes_total", 4, destination="openai", direction="received")
    assert not any(call.args[0] == "zont_proxy_failures_total" for call in observe.call_args_list)


def test_destination_and_size_denials_never_dispatch(monkeypatch):
    observe = Mock()
    monkeypatch.setattr(egress, "observe", observe)
    direct, proxied = Mock(), Mock()
    transport = egress.ReportTransport(direct=direct, proxied=proxied)
    with pytest.raises(ValueError):
        transport.handle_request(httpx.Request("GET", "https://private.invalid/secret"))
    observe.assert_any_call("zont_egress_denials_total", reason="destination")
    with pytest.raises(ValueError):
        transport.handle_request(httpx.Request("POST", "https://api.openai.com/v1/responses",
                                              content=b"x" * (egress.MAX_REPORT_REQUEST_BYTES + 1)))
    observe.assert_any_call("zont_egress_limit_total", direction="sent")
    direct.handle_request.assert_not_called()
    proxied.handle_request.assert_not_called()
    assert "private.invalid" not in str(observe.call_args_list)


def test_telemetry_freshness_uses_committed_sample_not_requested_end(monkeypatch):
    observe = Mock()
    monkeypatch.setattr(collection, "observe", observe)
    db, client = Mock(), Mock()
    end = datetime(2026, 1, 2, tzinfo=UTC)
    start = end - timedelta(minutes=30)
    sample_at = end - timedelta(minutes=5)
    db.list_devices.return_value = [{"id": "fixture"}]
    db.get_app_meta.return_value = None
    db.telemetry.missing_intervals.return_value = [(start.timestamp(), end.timestamp(), "missing")]
    config = AppConfig()
    config.zont.history_data_types = ["temperature"]
    client.load_history.return_value = [{"device_id": "fixture"}]
    client.normalize_history.return_value = ([SimpleNamespace(timestamp_utc=sample_at)], {})
    client.normalize_events.return_value = []
    result = collection.CollectionService(db, client, config).ensure_period(start, end, now=end)
    assert result["complete"]
    observe.assert_any_call("zont_telemetry_timestamp_seconds", sample_at.timestamp())
    observe.assert_any_call("zont_telemetry_lag_seconds", 300.0)
    db.telemetry.latest_timestamp.assert_not_called()


def test_openai_known_usage_counts_actual_tokens_on_invalid_response(monkeypatch):
    observe = Mock()
    monkeypatch.setattr(provider, "observe", observe)
    analyst = object.__new__(provider.OpenAIAnalyst)
    analyst.config = AppConfig()
    analyst.dispatch_deadline = None
    analyst.ledger = Mock()
    analyst.ledger.cached.return_value = None
    analyst.ledger.reserve.return_value = None
    analyst.client = Mock()
    analyst.client.responses.parse.return_value = SimpleNamespace(
        output_parsed=None,
        usage=SimpleNamespace(input_tokens=23, output_tokens=7,
                              input_tokens_details=SimpleNamespace(cached_tokens=11)),
    )
    with pytest.raises(RuntimeError):
        analyst.analyze({})
    observe.assert_any_call("zont_openai_tokens_total", 23, kind="input")
    observe.assert_any_call("zont_openai_tokens_total", 11, kind="cached")
    observe.assert_any_call("zont_openai_tokens_total", 7, kind="output")
    observe.assert_any_call("zont_openai_failures_total")
    analyst.ledger.finish_error.assert_called_once()
