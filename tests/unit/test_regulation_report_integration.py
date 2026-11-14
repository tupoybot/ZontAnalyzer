"""Public, synthetic fixtures for regulation observations in complete reports."""

from copy import deepcopy
from datetime import UTC, datetime

from zont_analyzer.domain import ObservedPattern, QualityResult, Report
from zont_analyzer.reports import render_html, render_text


def _report(*, context: dict | None = None, ai_used: bool = False,
            patterns: list[ObservedPattern] | None = None) -> Report:
    return Report(
        id="synthetic-regulation-report",
        kind="weekly",
        period_start=datetime(2026, 9, 1, tzinfo=UTC),
        period_end=datetime(2026, 9, 8, tzinfo=UTC),
        generated_at=datetime(2026, 9, 8, tzinfo=UTC),
        timezone="UTC",
        context=({"ai_provenance": {"prompt_version": "analyst-v8.14"}, **(context or {})}
                 if ai_used else context or {}),
        quality=QualityResult(score=0.8, coverage_pct=80, max_gap_seconds=0,
                              stuck_pct=0, implausible_jumps=0, sample_count=20),
        summary="Сводка синтетического периода.",
        observed_patterns=patterns or [],
        ai_used=ai_used,
    )


def _comparison(event_id: str, day: int, *, stage: str = "available") -> dict:
    return {
        "event_id": event_id,
        "stage": stage,
        "outcome": "improved" if stage == "available" else None,
        "event": {
            "event_id": event_id,
            "boundary": {"after": f"2026-09-{day:02d}T10:00:00+00:00",
                         "no_later_than": f"2026-09-{day:02d}T12:00:00+00:00"},
            "changed_values": {"pid_prop_koef": {
                "before": {"status": "observed", "value": day},
                "after": {"status": "observed", "value": day + 1},
            }},
        },
        "before": {"useful_hours": 10, "complete_cycles": 4},
        "after": {"useful_hours": 9 if stage == "available" else 0,
                  "complete_cycles": 3 if stage == "available" else 0},
        "metrics": {"room_absolute_error_c": {
            "before": 0.6, "after": 0.3, "direction": "improved"}},
        "limitations": (["subsequent_change_truncates_after_window"]
                        if stage == "superseded" else []),
    }


def _dense_context() -> dict:
    results = [_comparison(f"event-{day}", day, stage="superseded")
               for day in range(1, 5)]
    results.append(_comparison("event-5", 5))
    return {
        "regulation_comparisons": {"results": results},
        "control_settings_history": {"versions": [{
            "kind": "initial", "observed_at": "2026-09-01T00:00:00+00:00",
            "state": {"fields": {
                "pid_prop_koef": {"status": "observed", "value": 5},
                "pid_integral_koef": {"status": "observed", "value": 2},
            }},
        }, {
            "kind": "change", "event_id": "event-6",
            "observed_at": "2026-09-06T12:00:00+00:00",
            "boundary": {"after": "2026-09-06T10:00:00+00:00",
                         "no_later_than": "2026-09-06T12:00:00+00:00"},
            "changed_fields": ["pid_prop_koef", "pid_integral_koef"],
            "state": {"fields": {
                "pid_prop_koef": {"status": "observed", "value": 7},
                "pid_integral_koef": {"status": "observed", "value": 3},
            }},
        }]},
        "regulation_facts": {"circuits": [{
            "classification": "cycles",
            "coverage": {"eligible_seconds": 7200,
                         "exclusion_seconds": {"dhw": 900, "ventilation": 300}},
            "cycle_counts": {"eligible": 2, "partial": 1, "excluded": 2},
            "metrics": {"room_absolute_error_c": {
                "value": 0.4, "unit": "°C", "coverage_pct": 70},
                        "actual_range_c": {
                            "value": 8.2, "unit": "°C", "coverage_pct": 58},
                        "command_range_c": {
                            "value": 5.1, "unit": "°C", "coverage_pct": 91},
                        "cycle_mean_overshoot_c": {
                            "value": 0.8, "unit": "°C", "coverage_pct": 65},
                        **{f"unavailable_metric_{index}": {
                            "value": None, "coverage_pct": 0,
                            "unavailable_reasons": ["missing_data"]}
                           for index in range(40)}},
            "unknowns": ["missing_room_observations"],
        }]},
    }


def _observation_area(rendered: str) -> str:
    start = rendered.index('<section class="details-area full-width">')
    end = rendered.index('<section class="thermal-system full-width">', start)
    return rendered[start:end]


def test_dense_regulation_uses_concise_normal_observations_in_html_and_text() -> None:
    report = _report(context=_dense_context())
    saved = deepcopy(report.model_dump())
    plain = render_text(report)
    rendered = render_html(report)
    area = _observation_area(rendered)

    assert report.model_dump() == saved
    assert "Наблюдения" in plain and "Наблюдения" in area
    assert '<article class="reasoning-item observed">' in area
    assert 'class="regulation-result' not in rendered
    assert "Результат регулирования отопления" not in plain + rendered
    # The latest history change must not disappear behind earlier comparisons.
    assert "06.09.2026 12:00" in plain and "06.09.2026 12:00" in area
    assert "P 5 → 7" in plain and "P 5 → 7" in area
    assert "I 2 → 3" in plain and "I 2 → 3" in area
    assert "8,2 °C (покрытие 58%)" in plain and "8,2 °C (покрытие 58%)" in area
    assert "5,1 °C (покрытие 91%)" in plain and "5,1 °C (покрытие 91%)" in area
    # A dense input must not become a wall of superseded events or null metrics.
    assert "P 1 → 2" not in area
    assert "P 4 → 5" not in area
    assert "unavailable_metric_" not in plain + area
    assert "ПЗА" not in area
    observation_lines = plain.split("Наблюдения:", 1)[1].split("Гипотезы:", 1)[0]
    assert len(observation_lines.split()) < 220
    assert len(area.split()) < 220


def test_fresh_ai_observation_is_not_duplicated_by_regulation_fallback() -> None:
    statement = ("Измеренная температура подачи менялась, заданная температура подачи тоже менялась. "
                 "Настройки изменены с 10:00 до 12:00: P 5 → 7, I 2 → 3; сравнение ещё не готово.")
    report = _report(context=_dense_context(), ai_used=True,
                     patterns=[ObservedPattern(id="ai-pattern", statement=statement,
                                               epistemic_level="observed")])
    plain = render_text(report)
    rendered = render_html(report)
    area = _observation_area(rendered)

    assert plain.count(statement) == 1
    assert area.count(statement) == 1
    assert area.count('class="reasoning-item observed"') == 1
    assert "06.09.2026 12:00" not in area
    assert 'class="regulation-result' not in rendered


def test_fresh_ai_missing_flow_gets_only_missing_quantity() -> None:
    report = _report(context=_dense_context(), ai_used=True, patterns=[ObservedPattern(
        id="ai-observation", statement=("Измеренная температура подачи менялась. "
                                        "Настройки изменены с 10:00 до 12:00: P 5 → 7, I 2 → 3."),
        epistemic_level="derived")])
    rendered = _observation_area(render_html(report))
    assert rendered.count('class="reasoning-item observed"') == 2
    assert "размах заданной температуры подачи" in rendered
    assert "размах измеренной температуры подачи" not in rendered
    assert "абсолютное отклонение комнаты" not in rendered


def test_fresh_ai_unrelated_findings_keep_flow_and_latest_change_visible() -> None:
    report = _report(context=_dense_context(), ai_used=True, patterns=[ObservedPattern(
        id="ai-power", statement="Отмечена потеря электропитания.", epistemic_level="observed")])
    area = _observation_area(render_html(report))
    assert area.count('class="reasoning-item observed"') == 3
    assert "размах измеренной температуры подачи" in area
    assert "размах заданной температуры подачи" in area
    assert "I 2 → 3" in area
    assert "абсолютное отклонение комнаты" not in area


def test_stale_ai_is_archived_in_html_and_current_observation_is_visible() -> None:
    old_statement = "Старый вывод ИИ: <script>alert(1)</script>"
    context = _dense_context()
    context["control_settings_interpretation_stale"] = True
    report = _report(context=context, ai_used=True,
                     patterns=[ObservedPattern(id="old-ai-pattern", statement=old_statement,
                                               epistemic_level="observed")])
    saved = deepcopy(report.model_dump())
    plain = render_text(report)
    rendered = render_html(report)
    area = _observation_area(rendered)

    assert report.model_dump() == saved
    assert "AI-интерпретация устарела" in plain + rendered
    assert '<details class="previous-ai-reasoning">' in area
    assert "Прежнее объяснение ИИ" in area
    assert "06.09.2026 12:00" in plain and "06.09.2026 12:00" in area
    assert old_statement not in plain
    assert "<script>alert(1)</script>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in area
    assert area.index("Наблюдения") < area.index('class="previous-ai-reasoning"')
    assert 'class="regulation-result' not in rendered


def test_fallback_preserves_unrelated_existing_observation() -> None:
    existing = "Независимое наблюдение о синтетическом периоде."
    report = _report(context=_dense_context(),
                     patterns=[ObservedPattern(id="existing-pattern", statement=existing,
                                               epistemic_level="observed")])
    plain = render_text(report)
    area = _observation_area(render_html(report))

    assert plain.count(existing) == 1
    assert area.count(existing) == 1
    assert "06.09.2026 12:00" in plain and "06.09.2026 12:00" in area
    assert area.count('class="reasoning-item observed"') >= 2


def test_previous_prompt_does_not_hide_new_observation_logic_or_claim_changed_data() -> None:
    context = _dense_context()
    context["ai_provenance"] = {"prompt_version": "analyst-v8.13"}
    report = _report(context=context, ai_used=True, patterns=[ObservedPattern(
        id="previous", statement="Прежнее объяснение периода.", epistemic_level="derived")])
    plain, rendered = render_text(report), render_html(report)
    assert "предыдущей версии анализа" in plain
    assert "после обновления данных" not in plain
    assert report.summary not in plain
    assert "Прежняя сводка ИИ" in rendered and report.summary in rendered
    assert "I 2 → 3" in plain
    assert 'class="previous-ai-reasoning"' in rendered


def test_unknown_prompt_provenance_does_not_hide_current_facts() -> None:
    report = _report(context=_dense_context(), ai_used=True, patterns=[ObservedPattern(
        id="previous", statement="Прежнее объяснение периода.", epistemic_level="derived")])
    report.context.pop("ai_provenance")
    report.context["gas"] = {"ai_stale": True}
    text = render_text(report)
    assert "I 2 → 3" in text
    assert "Расход газа также пересчитан" in text
