from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser

from zont_analyzer.domain import DetectedEvent, MetricValue, QualityResult, Recommendation, Report
from zont_analyzer.reports import render_html


class Document(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, dict[str, str | None]]] = []
        self.debug_text: list[str] = []
        self.visible_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in {"meta", "input", "br", "hr", "link"}:
            self.stack.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if any(tag in {"script", "style"} for tag, _ in self.stack):
            return
        target = (
            self.debug_text
            if any("debug-only" in (attrs.get("class") or "").split() for _, attrs in self.stack)
            else self.visible_text
        )
        target.append(data)


def fixture_report(kind="daily") -> Report:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    return Report(
        id="internal-report-id",
        kind=kind,
        period_start=start,
        period_end=start + timedelta(days=1),
        generated_at=start + timedelta(days=2),
        summary="Система работает штатно",
        quality=QualityResult(
            score=0.9, coverage_pct=99, max_gap_seconds=60, stuck_pct=0, implausible_jumps=0, sample_count=100
        ),
        metrics=[MetricValue(id="internal-metric-id", name="mean_temperature_c", value=23.43, unit="°C")],
        events=[DetectedEvent(id=f"event-{i}", kind="burner_cycle", started_at=start) for i in range(60)],
        recommendations=[
            Recommendation(
                id="internal-rec-id",
                title="Наблюдать",
                category="observe_only",
                priority="low",
                confidence=0.9,
                hypothesis="Режим соответствует погоде",
                suggested_manual_action="Проверить завтра",
                expected_effect="Уточнить динамику",
                observation_period_days=1,
                evidence_metric_ids=["internal-metric-id"],
            )
        ],
    )


def test_debug_metadata_is_preserved_but_outside_normal_reading_flow():
    report = fixture_report()
    before = report.model_dump_json()
    page = render_html(report, {"internal-rec-id": {"status": "rejected", "owner_note": "<owner note>"}})
    document = Document()
    document.feed(page)
    normal, technical = "".join(document.visible_text), "".join(document.debug_text)
    for value in ("internal-report-id", "internal-metric-id", "internal-rec-id", "event-59"):
        assert value not in normal
        assert value in technical
    assert "23,4" in normal
    assert "<owner note>" in normal
    assert "Отклонено" in normal
    assert 'id="debug-toggle" type="checkbox"' in page
    assert "localStorage" in page and "get('debug')" in page
    assert "Показать ещё 60 технических событий" in normal
    assert report.model_dump_json() == before


def test_periods_share_components_and_keep_empty_states_honest():
    for kind in ("daily", "weekly", "monthly"):
        report = fixture_report(kind)
        page = render_html(report)
        assert 'class="hero success"' in page
        assert page.index("Что делать") < page.index("Подробные метрики")
        assert 'class="archive-picker"><summary>Архив' in page
        assert 'class="feedback-comment"><summary>' in page
        assert 'class="metric-group"><summary>Комфорт' in page
        assert "Нет данных" in page
        assert "Grafana" not in page
        report.quality.score = 0.2
        assert "Недостаточно данных для оценки" in render_html(report)


def test_detailed_metrics_follow_physical_scope_in_every_report_kind():
    from zont_analyzer.reports.presentation import metric_groups

    cases = {
        "mean_temperature_c": "Комфорт и отопление",
        "heating_target_evaluation_time_pct": "Комфорт и отопление",
        "outdoor_mean_temperature_c": "Погода",
        "dhw_burner_starts": "ГВС",
        "dhw_mean_recovery_minutes": "ГВС",
        "dhw_confirmed_heating_pause_count": "Взаимодействие ГВС и отопления",
        "dhw_mean_heating_return_delay_minutes": "Взаимодействие ГВС и отопления",
        "boiler_uptime_seconds": "Надёжность",
        "unconfirmed_burner_pulse_count": "Качество данных",
        "unknown_future_metric": "Другие показатели",
        "burner_starts": "Котёл и горелка",
    }
    for kind in ("daily", "weekly", "monthly", "seasonal"):
        report = fixture_report(kind)
        report.metrics = [MetricValue(id=name, name=name, value=1, unit="") for name in cases]
        report.context["gas"] = {"flame_hours": 2, "heating_flame_hours": 1, "dhw_flame_hours": 1,
                                 "purpose_unknown_flame_hours": 0, "flame_observed_hours": 24}
        output = metric_groups(report, None)
        sections = output.split('<details class="metric-group">')[1:]
        for name, group in cases.items():
            matching = [section for section in sections if f'"name": "{name}"' in section.replace('&quot;', '"')]
            assert len(matching) == 1, name
            assert matching[0].startswith(f"<summary>{group}</summary>"), name
        boiler = next(section for section in sections if section.startswith("<summary>Котёл и горелка</summary>"))
        for label in ("Время работы горелки", "Доля горения на отопление", "Доля горения на ГВС"):
            assert label in boiler
        report.metrics[-1].context["activity_scope"] = "space_heating_only"
        scoped = metric_groups(report, None).split('<details class="metric-group">')[1]
        assert "Запуски горелки на отопление" in scoped


def test_recalculated_setpoints_do_not_claim_ai_was_regenerated():
    report = fixture_report().model_copy(update={"ai_used": True})
    report.context["setpoint_recalculation"] = {"version": "setpoints-v1"}
    assert "Текст AI сохранён из предыдущей версии отчёта" in render_html(report)
