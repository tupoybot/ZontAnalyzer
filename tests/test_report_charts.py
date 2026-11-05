import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.ydb_support import make_database
from zont_analyzer.domain import QualityResult, Report
from zont_analyzer.reports import chart_data
from zont_analyzer.reports.charts import render_charts


def _report() -> Report:
    start = datetime(2026, 9, 5, tzinfo=UTC)
    return Report(
        id="chart-test", kind="daily", period_start=start, period_end=start + timedelta(days=1),
        generated_at=start, timezone="Europe/Samara", summary="summary",
        quality=QualityResult(score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
                              implausible_jumps=0, sample_count=1),
    )


def test_charts_use_only_timestamped_observations_and_show_unavailable_old_reports() -> None:
    rendered = render_charts(_report())

    assert rendered.count('report-chart--unavailable') == 2
    assert "Сводные метрики и почасовые агрегаты не отображаются как кривая" in rendered
    assert "<svg" not in rendered


def test_charts_render_real_series_split_gaps_and_explicit_state_bands() -> None:
    rendered = render_charts(_report(), {
        "timezone": "Europe/Samara",
        "series": {
            "control_temperature": {"label": "Гостиная", "unit": "°C", "points": [
                {"timestamp": "2026-09-05T00:00:00+00:00", "value": 21.0},
                {"timestamp": "2026-09-05T00:10:00+00:00", "value": 21.2},
                {"timestamp": "2026-09-05T00:20:00+00:00", "value": 21.15},
                {"timestamp": "2026-09-05T02:00:00+00:00", "value": 21.1},
            ]},
            "target_temperature": {"label": "Цель", "unit": "°C", "points": [
                {"timestamp": "2026-09-05T00:00:00+00:00", "value": 22.0},
                {"timestamp": "2026-09-05T02:00:00+00:00", "value": 22.0},
            ]},
            "flow_temperature": {"label": "Подача", "unit": "°C", "points": [
                {"timestamp": "2026-09-05T00:00:00+00:00", "value": 36.0},
                {"timestamp": "2026-09-05T02:00:00+00:00", "value": 39.0},
            ]},
            "dhw_temperature": {"label": "БКН", "unit": "°C", "points": [
                {"timestamp": "2026-09-05T00:00:00+00:00", "value": 48.0},
                {"timestamp": "2026-09-05T02:00:00+00:00", "value": 49.0},
            ]},
        },
        "state_bands": [{"started_at": "2026-09-05T00:15:00+00:00", "ended_at": "2026-09-05T00:35:00+00:00",
                         "label": "ГВС", "state": "dhw"}],
    })

    assert rendered.count('<svg class="chart-svg"') == 2
    assert 'data-chart="climate"' in rendered and 'data-chart="thermal"' in rendered
    assert "Гостиная" in rendered and "разрывы: 1" in rendered
    assert "<title>ГВС: dhw</title>" in rendered
    assert 'class="chart-state-legend"' in rendered
    assert "Пламя ГВС: ГВС" in rendered
    assert "Нет ряда: Улица." in rendered
    assert 'data-role="target_temperature"' in rendered and 'stroke-dasharray="7 5"' in rendered
    assert 'data-role="dhw_temperature"' in rendered and "БКН" in rendered
    assert 'aria-labelledby="climate-title climate-desc"' in rendered
    assert 'class="chart-unit">°C</div>' in rendered
    assert ">04:00</span>" in rendered and ">10:00</span>" in rendered


def test_charts_discard_invalid_points_and_escape_untrusted_labels() -> None:
    rendered = render_charts(_report(), {"series": {
        "control_temperature": {"label": "<script>x</script>", "points": [
            {"timestamp": "2026-09-05T00:00:00+00:00", "value": 20},
            {"timestamp": "not-a-time", "value": 99},
            {"timestamp": "2026-09-05T01:00:00+00:00", "value": 21},
        ]},
    }})

    assert "&lt;script&gt;x&lt;/script&gt;" in rendered
    assert "<script>x</script>" not in rendered
    assert "99.0" not in rendered


def test_chart_data_gap_marker_is_never_joined_after_decimation() -> None:
    rendered = render_charts(_report(), {"series": {
        "control_temperature": {"points": [
            {"timestamp": "2026-09-05T00:00:00+00:00", "value": 20},
            {"timestamp": "2026-09-05T12:00:00+00:00", "value": 21, "gap_before": True},
        ]},
    }})

    climate = rendered.split('data-chart="thermal"', 1)[0]
    assert climate.count('<path data-role="control_temperature"') == 2
    assert "разрывы: 1" in climate


def test_explicit_setpoint_packet_does_not_infer_gap_and_renders_step() -> None:
    rendered = render_charts(_report(), {"gap_policy": "explicit", "series": {
        "target_temperature": {"points": [
            {"timestamp": "2026-09-05T00:00:00+00:00", "value": 22},
            {"timestamp": "2026-09-05T04:00:00+00:00", "value": 22},
            {"timestamp": "2026-09-05T08:00:00+00:00", "value": 23},
        ], "interpolation": "step"},
    }})
    climate = rendered.split('data-chart="thermal"', 1)[0]
    assert "разрывы:" not in climate
    assert climate.count('<path data-role="target_temperature"') == 1


def test_requested_panel_can_be_rendered_independently() -> None:
    rendered = render_charts(_report(), panel_ids=("climate",))

    assert 'data-chart="climate"' in rendered
    assert 'data-chart="thermal"' not in rendered


@pytest.mark.ydb
def test_rebind_chart_cache_reuses_packet_for_gas_only_refresh(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = make_database(tmp_path)
    original = _report()
    original.context.update({"gas": {"coefficient": 1}, "calculation_version": "v1"})
    refreshed = original.model_copy(deep=True)
    refreshed.context.update({"gas": {"coefficient": 2}, "gas_savings": {"value": 3},
                             "gas_interpretation_stale": True, "calculation_version": "v2"})
    packet = {"timezone": "UTC", "series": {}}
    monkeypatch.setattr(chart_data, "build_chart_data", lambda *_args: packet)
    assert chart_data.cached_chart_data(db, original) == packet

    def no_build(*_args):
        raise AssertionError("rebound cache must not build chart data")

    monkeypatch.setattr(chart_data, "build_chart_data", no_build)
    assert chart_data.rebind_chart_cache(db, original, refreshed) is True
    assert chart_data.cached_chart_data(db, refreshed) == packet


@pytest.mark.ydb
def test_rebind_chart_cache_refuses_non_gas_changes(tmp_path) -> None:
    db = make_database(tmp_path)
    original = _report()
    refreshed = original.model_copy(update={"summary": "changed"})

    assert chart_data.rebind_chart_cache(db, original, refreshed) is False


@pytest.mark.parametrize("cache_state", ["missing", "corrupt"])
@pytest.mark.ydb
def test_rebind_chart_cache_handles_missing_or_corrupt_cache(tmp_path, cache_state) -> None:
    db = make_database(tmp_path)
    original = _report()
    refreshed = original.model_copy(update={"context": {"gas": {"value": 1}}})
    if cache_state == "corrupt":
        key, _digest = chart_data._cache_key(original)
        db.set_app_meta(key, "not json")

    assert chart_data.rebind_chart_cache(db, original, refreshed) is False


@pytest.mark.ydb
def test_rebind_legacy_chart_packet_after_saved_gas_refresh(tmp_path, monkeypatch) -> None:
    db = make_database(tmp_path)
    original = _report()
    db.save_report(original, original.summary)
    key, digest = chart_data._cache_key(original)
    legacy_key = f"chart-data:v2:{key.rsplit(':', 1)[1]}:{digest}"
    packet = {"timezone": "UTC", "series": {"retained": {"points": [1, 2]}}}
    db.set_app_meta(legacy_key, json.dumps({
        "schema_version": chart_data.CHART_DATA_SCHEMA_VERSION,
        "report_digest": digest, "data": packet,
    }))

    def no_build(*_args):
        raise AssertionError("gas refresh must not read telemetry")

    monkeypatch.setattr(chart_data, "build_chart_data", no_build)
    refreshed = original.model_copy(update={"context": {"gas": {"revision": 1}}})
    db.save_report(refreshed, refreshed.summary)
    assert chart_data.rebind_chart_cache(db, original, refreshed)
    assert chart_data.cached_chart_data(db, refreshed) == packet

    newer = refreshed.model_copy(update={"context": {"gas": {"revision": 2}}})
    db.save_report(newer, newer.summary)
    assert chart_data.rebind_chart_cache(db, refreshed, newer)
    encoded = db.get_app_meta(key)
    assert chart_data.rebind_chart_cache(db, original, refreshed) is False
    assert db.get_app_meta(key) == encoded


@pytest.mark.ydb
@pytest.mark.parametrize("source", ["v3", "legacy"])
def test_overlapping_saved_gas_refreshes_carry_packet_atomically(tmp_path, monkeypatch, source) -> None:
    db = make_database(tmp_path)
    original = _report()
    db.save_report(original, original.summary)
    packet = {"timezone": "UTC", "series": {"retained": {"points": [1, 2]}}}
    key, digest = chart_data._cache_key(original)
    source_key = key if source == "v3" else chart_data._legacy_cache_key(original)
    source_payload = json.dumps({
        "schema_version": chart_data.CHART_DATA_SCHEMA_VERSION,
        "report_digest": digest, "data": packet,
    })
    db.set_app_meta(source_key, source_payload)

    def no_build(*_args):
        raise AssertionError("overlapping gas refreshes must retain the original telemetry packet")

    monkeypatch.setattr(chart_data, "build_chart_data", no_build)
    middle = original.model_copy(update={"context": {"gas": {"revision": 1}}})
    latest = original.model_copy(update={"context": {"gas": {"revision": 2}}})
    assert db.reports.replace_context(original, middle, "gas-test:original", "original")
    assert db.reports.replace_context(middle, latest, "gas-test:middle", "middle")
    # Both canonical commits finish before either worker's post-save callback.
    assert chart_data.rebind_chart_cache(db, middle, latest)
    encoded = db.get_app_meta(key)
    assert not chart_data.rebind_chart_cache(db, original, middle)
    assert db.get_app_meta(key) == encoded
    assert chart_data.cached_chart_data(db, latest) == packet
    assert db.report(original.id) == latest
    rows = db.storage.execute("SELECT key FROM app_meta WHERE key LIKE 'chart-data:%';")[0].rows
    assert len(rows) == (1 if source == "v3" else 2)
    if source == "legacy":
        assert db.get_app_meta(source_key) == source_payload

    def no_write(*_args):
        raise AssertionError("an already carried packet needs no post-save write")

    monkeypatch.setattr(db, "set_report_chart_cache", no_write)
    assert chart_data.rebind_chart_cache(db, middle, latest)


@pytest.mark.ydb
def test_saved_non_gas_context_change_does_not_carry_chart_packet(tmp_path, monkeypatch) -> None:
    db = make_database(tmp_path)
    original = _report()
    db.save_report(original, original.summary)
    packet = {"timezone": "UTC", "series": {}}
    monkeypatch.setattr(chart_data, "build_chart_data", lambda *_args: packet)
    assert chart_data.cached_chart_data(db, original) == packet
    key, _digest = chart_data._cache_key(original)
    original_encoded = db.get_app_meta(key)
    changed = original.model_copy(update={"context": {"temporal_evidence": {"revision": 2}}})
    assert db.reports.replace_context(original, changed, "non-gas-test", "original")
    assert db.get_app_meta(key) == original_encoded
    assert not chart_data.rebind_chart_cache(db, original, changed)
