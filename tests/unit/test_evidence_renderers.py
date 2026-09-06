from datetime import UTC, datetime

from zont_analyzer.domain import QualityResult, Report
from zont_analyzer.reports import render_html, render_text


def _report(packet: dict | None = None) -> Report:
    context = {"temporal_evidence": packet} if packet is not None else {}
    return Report(
        id="r-evidence",
        kind="daily",
        period_start=datetime(2026, 9, 1, tzinfo=UTC),
        period_end=datetime(2026, 9, 2, tzinfo=UTC),
        generated_at=datetime(2026, 9, 2, tzinfo=UTC),
        timezone="Europe/Samara",
        context=context,
        quality=QualityResult(
            score=1,
            coverage_pct=100,
            max_gap_seconds=0,
            stuck_pct=0,
            implausible_jumps=0,
            sample_count=10,
        ),
        summary="summary",
    )


def test_old_report_without_temporal_context_is_unchanged() -> None:
    text = render_text(_report())
    rendered = render_html(_report())
    assert "Временные свидетельства" not in text
    assert '<section class="temporal-evidence">' not in rendered


def test_temporal_evidence_text_shows_kpi_quality_and_exact_window() -> None:
    packet = {
        "algorithm_version": "heating-evidence-v1",
        "period_start": "2026-09-01T00:00:00+00:00",
        "period_end": "2026-09-02T00:00:00+00:00",
        "timezone": "Europe/Samara",
        "capability_profile": "unknown",
        "signals": {
            "room": {"display_name": "Гостиная", "role": "room", "unit": "°C"},
        },
        "quality": {
            "room": {"mean": 21.5, "minimum": 20, "maximum": 22, "coverage_pct": 98, "sample_count": 4,
                     "source": "observed"},
        },
        "metrics": [{
            "id": "burner_starts_per_hour",
            "name": "Запуски горелки в час",
            "value": None,
            "unit": "1/ч",
            "source": "derived",
            "denominator": 0,
            "denominator_unit": "ч активного запроса",
            "coverage_pct": 12.5,
            "unavailable_reason": "нет активного запроса",
        }],
        "windows": [{
            "id": "period:hour:2026-09-01T00:00:00+04:00",
            "started_at": "2026-08-31T20:00:00+00:00",
            "ended_at": "2026-08-31T21:00:00+00:00",
            "timezone": "Europe/Samara",
            "tags": ["night"],
            "signals": {"room": {"mean": 21.5, "minimum": 20, "maximum": 22, "coverage_pct": 98}},
            "facts": {"room_error_c": {"mean": -0.5, "change": 0.2, "gaps": 1}},
        }],
        "exclusions": {},
        "exclusion_windows": [{
            "id": "exclusion:period:dhw:1:2",
            "started_at": "2026-08-31T20:10:00+00:00",
            "ended_at": "2026-08-31T20:20:00+00:00",
            "reason": "dhw",
            "source": "derived",
        }],
        "unknowns": [],
    }
    text = render_text(_report(packet))
    assert "нет активного запроса" in text
    assert "знаменатель 0 ч активного запроса" in text
    assert "2026-09-01 00:00:00 — 2026-09-01 01:00:00" in text
    assert "Гостиная" in text and "среднее 21.5" in text and "изменение 0.2" in text
    assert "Качество по источникам" in text and "приоритет ГВС" in text


def test_temporal_evidence_html_escapes_values_and_is_collapsed() -> None:
    packet = {
        "timezone": "UTC",
        "signals": {"x": {"display_name": "<script>alert(1)</script>", "role": "room", "unit": "°C"}},
        "windows": [],
        "metrics": [],
    }
    rendered = render_html(_report(packet))
    assert '<script>alert(1)</script>' not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert '<details><summary>Временные свидетельства' in rendered
