"""Render identical synthetic reports with accepted and candidate renderers.

No AI, database, browser or network is used. Supply the accepted renderers.py
as --baseline-renderer; shared presentation dependencies remain current. This
is a paired presentation check, not an old AnalysisService or model comparison.
The empty output directory must be outside the repository. Artifacts are private.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import inspect
import json
import math
import os
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from tests.regulation_acceptance_cases import (
    SCENARIOS,
    START,
    TRIANGLE_ORACLE,
    change_timeline,
    comparison_facts,
    daily_facts,
    late_peak,
    triangle_inputs,
    ventilation_inputs,
)
from zont_analyzer.adapters.openai.provider import analysis_packet
from zont_analyzer.analytics.regulation import aggregate_regulation_facts, build_regulation_facts
from zont_analyzer.analytics.regulation_comparison import compare_change, observed_changes
from zont_analyzer.domain import MetricValue, QualityResult, Report
from zont_analyzer.reports import render_html, render_text
from zont_analyzer.reports.regulation import regulation_observations


@dataclass
class Case:
    name: str
    family: int
    report: Report
    oracle: dict[str, Any]
    requirements: dict[str, tuple[str, ...]]


def _report(name: str, context: dict[str, Any], *, days: int = 1, start=START, hours: int | None = None) -> Report:
    end = start + timedelta(hours=hours if hours is not None else days * 24)
    return Report(id="synthetic-" + name, kind="seasonal" if days > 31 else "weekly" if days == 7 else "daily",
                  period_start=start, period_end=end, generated_at=end, timezone="UTC", context=context,
                  summary="Синтетическая проверка отопления без AI.",
                  quality=QualityResult(score=1, coverage_pct=100, max_gap_seconds=300, stuck_pct=0,
                                        implausible_jumps=0, sample_count=days * 288))


def _context(fact: dict[str, Any]) -> dict[str, Any]:
    return {"regulation_facts": {"circuits": [fact], "incomplete": False}}


def _compared(*, room=.5, flow=2.0, confound=None, second=False, short=False, manual=None):
    timeline = change_timeline(second=second)
    joined = observed_changes(timeline, [manual] if manual else [])
    facts = comparison_facts(room_scale=room, flow_scale=flow, confound=confound)
    if short or second:
        after_start = START + timedelta(hours=25)
        facts[1] = build_regulation_facts(start=after_start, end=START + timedelta(hours=27),
                                          inputs=triangle_inputs(start=after_start, room_scale=room,
                                                                 flow_scale=flow, version="v2"))
    result = compare_change(joined["events"][0], events=joined["events"], facts=facts,
                            start=START, end=START + timedelta(hours=49))
    context = _context(facts[1])
    context["regulation_facts"]["incomplete"] = short or second
    context.update(control_settings_history=timeline, regulation_comparisons={
        "results": [result], "history_start": START.isoformat(),
        "history_end": (START + timedelta(hours=49)).isoformat()})
    return context


def cases() -> list[Case]:
    causal = {"joint_effect_only": ("отдельный эффект", "не установлен"),
              "causal_restraint": ("не доказательство причины", "экономия газа")}
    daily = daily_facts(triangle_inputs(), 1)[0]
    repeated = _report("repeated-overshoot", _context(daily))
    repeated.metrics = [MetricValue(id="synthetic-mean", name="mean_temperature_c", value=20.1, unit="°C"),
                        MetricValue(id="synthetic-mae", name="mean_absolute_target_error_c", value=5 / 14, unit="°C")]
    result = [Case("repeated-overshoot", 1, repeated,
                   {"metrics": TRIANGLE_ORACLE, "full": 7, "partial": 1, "eligible_seconds": 86400},
                   {"overshoot": ("выбег", "0,8"), "post_heat_rise": ("после снятия запроса", "0,35"),
                    "day_limit": ("одних суток недостаточно",), "water_not_floor": ("поверхности пола",)})]
    for humidity in ("same", "missing", "other"):
        fact = build_regulation_facts(start=START, end=START + timedelta(hours=2),
                                       inputs=ventilation_inputs(humidity))
        result.append(Case("ventilation-" + humidity, 2, _report("ventilation-" + humidity, _context(fact), hours=2),
                           {"metrics": {"room_absolute_error_c": 0}, "eligible_seconds": 4500,
                            "recovery_reason": "ventilation_corroborated" if humidity == "same"
                            else "abrupt_room_cooling"},
                           {"impact_and_recovery": ("00:30", "01:15", "восстановление"),
                            "evidence_strength": ("проветривание",) if humidity == "same"
                            else ("причина не подтверждена",)}))
    improved = _compared(flow=.5)
    result.append(Case("zont-only-joint-change", 3,
                       _report("zont-only-joint-change", improved, start=START + timedelta(hours=25)),
                       {"stage": "available", "outcome": "improved", "comparison_mae_after": 5 / 28,
                        "comparison_metrics": {"room_absolute_error_c": [5 / 14, 5 / 28],
                                               "actual_variation_c_per_hour": [40 / 3, 20 / 3]}},
                       {**causal, "old_new_values": ("1 → 3", "2 → 4"),
                        "uncertain_time": ("точное время неизвестно",)}))
    forecast = {"goal": "Стабильная температура комнаты", "observation_window": "7 дней",
                "expected_effect": "Среднее абсолютное отклонение уменьшится минимум на 0,1 °C за семь дней",
                "predictions": []}
    manual = {"intervention_id": "synthetic-journal", "recommendation_id": None, "experiment": {
        "category": "settings", "performed_at": (START + timedelta(hours=24, minutes=30)).isoformat(),
        "original": forecast, "changes": [
            {"device_id": "synthetic-device", "circuit_id": "synthetic-room", "parameter": key,
             "before": str(old), "after": str(new)}
            for key, old, new in (("pid_prop_koef", 1, 3), ("pid_integral_koef", 2, 4))]}}
    journal = _compared(manual=manual)
    result.append(Case("independent-experiment", 4,
                       _report("independent-experiment", journal, start=START + timedelta(hours=25)),
                       {"stage": "available", "outcome": "mixed", "original_forecast": forecast,
                        "comparison_metrics": {"room_absolute_error_c": [5 / 14, 5 / 28],
                                               "actual_variation_c_per_hour": [40 / 3, 80 / 3]}},
                       {**causal, "frozen_forecast": (forecast["expected_effect"].lower(), "7 дней"),
                        "timing_restraint": ("не считается прогнозом, зафиксированным заранее",)}))
    variants = [("mixed", {}, "available", "mixed"),
                ("indistinguishable", {"room": 1., "flow": 1.}, "available", "indistinguishable"),
                ("worsened", {"room": 1.5, "flow": 2.}, "available", "worsened"),
                ("weather", {"confound": "weather"}, "insufficient", None),
                ("target", {"confound": "target"}, "insufficient", None),
                ("short", {"short": True}, "insufficient", None),
                ("second-change", {"second": True}, "superseded", None)]
    for name, options, stage, outcome in variants:
        context = _compared(**options)
        requirements = {**causal}
        if name == "mixed":
            requirements["mixed_not_success"] = ("разнонаправленные изменения", "ухудшение", "улучшение")
        elif name in {"weather", "target", "short"}:
            requirements["insufficient_not_success"] = ("недостаточно", "пока не установлен")
        elif name == "second-change":
            requirements["second_action_limits_first"] = ("следующим изменением", "пока не установлен")
        report = _report(name, context, start=START + timedelta(hours=25))
        oracle = {"stage": stage, "outcome": outcome}
        if options.get("short") or options.get("second"):
            report.quality.coverage_pct = 100 * 2 / 24
            oracle.update(full=0, partial=1, eligible_seconds=7200)
        elif name in {"mixed", "indistinguishable", "worsened"}:
            oracle["comparison_metrics"] = {
                "room_absolute_error_c": [5 / 14, options.get("room", .5) * 5 / 14],
                "actual_variation_c_per_hour": [40 / 3, options.get("flow", 2.) * 40 / 3]}
        result.append(Case(name, 5, report, oracle, requirements))
    source = triangle_inputs(60)
    season = aggregate_regulation_facts(daily_facts(source, 60), start=START,
                                        end=START + timedelta(days=60), expected_days=60)
    result.append(Case("complete-season", 6, _report("complete-season", _context(season), days=60),
                       {"metrics": TRIANGLE_ORACLE, "full": 479, "partial": 1, "eligible_seconds": 5184000},
                       {"full_census": ("479", "неполных 1"),
                        "observed_weather_only": ("реально наблюдавшимся", "отсутствующие диапазоны не проверены")}))
    late = aggregate_regulation_facts(daily_facts(late_peak(source), 60), start=START,
                                      end=START + timedelta(days=60), expected_days=60)
    result.append(Case("late-season", 6, _report("late-season", _context(late), days=60),
                       {"metrics": {"room_signed_error_c": .1 + 30 / 5184000,
                                    "cycle_mean_overshoot_c": .8 + .1 / 479},
                        "full": 479, "partial": 1, "eligible_seconds": 5184000},
                       {"full_census": ("479", "неполных 1")}))
    return result


def verify_oracle(case: Case) -> None:
    context, oracle = case.report.context, case.oracle
    fact = context["regulation_facts"]["circuits"][0]
    for name, expected in oracle.get("metrics", {}).items():
        assert math.isclose(fact["metrics"][name]["value"], expected, abs_tol=1e-8), (case.name, name)
    for name in ("full", "partial"):
        if name in oracle:
            assert fact["cycle_counts"][name] == oracle[name], (case.name, name)
    if "eligible_seconds" in oracle:
        assert fact["coverage"]["eligible_seconds"] == oracle["eligible_seconds"], case.name
    if "recovery_reason" in oracle:
        window = fact["exclusion_windows"][0]
        assert window["reason"] == oracle["recovery_reason"] and window["recovered"] is True
        assert window["started_at"] == (START + timedelta(minutes=30)).isoformat()
        assert window["ended_at"] == (START + timedelta(minutes=75)).isoformat()
    if "stage" in oracle:
        comparison = context["regulation_comparisons"]["results"][0]
        assert comparison["stage"] == oracle["stage"] and comparison["outcome"] == oracle["outcome"], case.name
        if "comparison_mae_after" in oracle:
            assert math.isclose(comparison["metrics"]["room_absolute_error_c"]["after"],
                                oracle["comparison_mae_after"], abs_tol=1e-8)
        if "original_forecast" in oracle:
            assert comparison["forecast_assessments"][0]["original"] == oracle["original_forecast"]
        for name, (before, after) in oracle.get("comparison_metrics", {}).items():
            metric = comparison["metrics"][name]
            assert math.isclose(metric["before"], before, abs_tol=1e-8), (case.name, name)
            assert math.isclose(metric["after"], after, abs_tol=1e-8), (case.name, name)


def _write(path: Path, value: str) -> None:
    with path.open("x", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        handle.write(value)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-renderer", type=Path, required=True)
    parser.add_argument("--baseline-label", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output_dir.resolve()
    if output.is_relative_to(root) or (output.exists() and any(output.iterdir())):
        parser.error("--output-dir must be empty and outside the repository")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(output, 0o700)
    spec = importlib.util.spec_from_file_location("zont_analyzer.reports._accepted_regulation_renderer",
                                                 args.baseline_renderer)
    if spec is None or spec.loader is None:
        parser.error("cannot load accepted renderer")
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    manifest = {"baseline_label": args.baseline_label,
                "baseline_renderer_sha256": hashlib.sha256(args.baseline_renderer.read_bytes()).hexdigest(),
                "candidate_sources_sha256": {
                    name: hashlib.sha256(Path(inspect.getfile(component)).read_bytes()).hexdigest()
                    for name, component in (("renderers", render_text), ("regulation_view", regulation_observations),
                                            ("regulation_facts", build_regulation_facts),
                                            ("comparison", compare_change), ("packet", analysis_packet))},
                "scope": "paired presentation; same Report input; current shared helpers; no AI/database/runtime claim",
                "external_ai_calls": 0, "families": list(SCENARIOS), "cases": []}
    for case in cases():
        verify_oracle(case)
        before = deepcopy(case.report)
        old_text, new_text = baseline.render_text(case.report), render_text(case.report)
        old_html, new_html = baseline.render_html(case.report), render_html(case.report)
        assert case.report == before, "rendering mutated the shared report input"
        for suffix, content in (("old.txt", old_text), ("new.txt", new_text),
                                ("old.html", old_html), ("new.html", new_html),
                                ("input.json", case.report.model_dump_json(indent=2))):
            _write(output / f"{case.name}.{suffix}", content)
        checks = {variant: {name: all(token.lower() in text.lower() for token in tokens)
                            for name, tokens in case.requirements.items()}
                  for variant, text in (("old", old_text), ("new", new_text))}
        observations = regulation_observations(case.report)
        assert observations, case.name
        observation_words = sum(len(item.statement.split()) for item in observations)
        observation_section = new_html.split('<section class="details-area full-width">', 1)[1].split(
            '<section class="thermal-system full-width">', 1)[0]
        assert "Наблюдения:" in new_text and "<h2>Наблюдения</h2>" in observation_section, case.name
        assert observation_section.count('class="reasoning-item observed"') == len(observations), case.name
        assert 'class="regulation-result' not in new_html, case.name
        assert "Результат регулирования отопления" not in new_text + new_html, case.name
        assert observation_words <= 220, (case.name, observation_words)
        manifest["cases"].append({"name": case.name, "family": case.family, "independent_oracle_passed": True,
                                  "oracle": case.oracle,
                                  "presentation_acceptance": {"observation_count": len(observations),
                                                              "observation_words": observation_words,
                                                              "normal_placement": True,
                                                              "no_standalone_section": True,
                                                              "input_unchanged": True},
                                  "historical_narrative_diagnostic": checks})
        if case.name == "independent-experiment":
            daily_report = case.report.model_copy(deep=True, update={
                "id": "synthetic-daily-composite", "kind": "daily", "period_start": START + timedelta(hours=25)})
            packet = analysis_packet(quality=daily_report.quality.model_dump(mode="json"), metrics=[], events=[],
                                     period={"kind": "daily", "start": daily_report.period_start.isoformat(),
                                             "end": daily_report.period_end.isoformat(), "timezone": "UTC"},
                                     context=daily_report.context)
            _write(output / "daily-model-composite-report.json", daily_report.model_dump_json(indent=2))
            _write(output / "daily-model-composite-packet.json", _json(packet))
    _write(output / "manifest.json", _json(manifest))
    print(_json({"cases": len(manifest["cases"]), "families": 6,
                 "independent_oracles_passed": True,
                 "presentation_acceptance_passed": True}))


if __name__ == "__main__":
    main()
