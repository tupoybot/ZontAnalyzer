from datetime import UTC, datetime, timedelta

import pytest

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


def test_requested_panel_can_be_rendered_independently() -> None:
    rendered = render_charts(_report(), panel_ids=("climate",))

    assert 'data-chart="climate"' in rendered
    assert 'data-chart="thermal"' not in rendered


def test_rebind_chart_cache_reuses_packet_for_gas_only_refresh(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = chart_data.Database(tmp_path / "state.sqlite3")
    db.initialize()
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


def test_rebind_chart_cache_refuses_non_gas_changes(tmp_path) -> None:
    db = chart_data.Database(tmp_path / "state.sqlite3")
    db.initialize()
    original = _report()
    refreshed = original.model_copy(update={"summary": "changed"})

    assert chart_data.rebind_chart_cache(db, original, refreshed) is False


@pytest.mark.parametrize("cache_state", ["missing", "corrupt"])
def test_rebind_chart_cache_handles_missing_or_corrupt_cache(tmp_path, cache_state) -> None:
    db = chart_data.Database(tmp_path / "state.sqlite3")
    db.initialize()
    original = _report()
    refreshed = original.model_copy(update={"context": {"gas": {"value": 1}}})
    if cache_state == "corrupt":
        path, _digest = chart_data._cache_path(db, original)
        path.parent.mkdir(parents=True)
        path.write_text("not json", encoding="utf-8")

    assert chart_data.rebind_chart_cache(db, original, refreshed) is False
