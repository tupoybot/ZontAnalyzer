from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import MethodType
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from zont_analyzer.application import feedback, regeneration
from zont_analyzer.application.analysis import AnalysisService
from zont_analyzer.application.feedback import build_feedback_server
from zont_analyzer.domain import DetectedEvent, MetricValue
from zont_analyzer.reports.regeneration import render_regeneration
from zont_analyzer.runtime import build_runtime


def _runtime(tmp_path: Path):
    runtime = build_runtime(None, tmp_path)
    report = AnalysisService(runtime.db, runtime.config).analyze_daily(
        datetime(2026, 8, 1, tzinfo=UTC).date(), use_ai=False
    )
    return runtime, report


def _wait(runtime, report_id: str) -> dict:
    for _ in range(100):
        value = regeneration.status(runtime, report_id)
        if value.get("status") in {"success", "error"}:
            return value
        time.sleep(0.01)
    raise AssertionError("regeneration did not finish")


def test_duplicate_clicks_share_one_active_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, report = _runtime(tmp_path)
    monkeypatch.setattr(regeneration, "_publish_locked", lambda *_args, **_kwargs: {})
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def regenerate(self, old, **kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(2)
        return old.model_copy(update={"generated_at": datetime.now(UTC)})

    service = AnalysisService(runtime.db, runtime.config)
    service.regenerate = MethodType(regenerate, service)  # type: ignore[method-assign]
    monkeypatch.setattr(runtime, "analysis", lambda **_: service)
    first = regeneration.start(runtime, report.id)
    assert first["status"] in {"queued", "running"}
    assert entered.wait(1)
    second = regeneration.start(runtime, report.id)
    assert second["status"] == "running"
    assert calls == 1
    release.set()
    assert _wait(runtime, report.id)["status"] == "success"


def test_ai_or_budget_error_preserves_canonical_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, report = _runtime(tmp_path)
    before = runtime.db.report(report.id).model_dump_json()
    service = AnalysisService(runtime.db, runtime.config)

    def fail(self, old, **kwargs):
        raise RuntimeError("Monthly OpenAI token budget is exhausted")

    service.regenerate = MethodType(fail, service)  # type: ignore[method-assign]
    monkeypatch.setattr(runtime, "analysis", lambda **_: service)
    regeneration.start(runtime, report.id)
    assert _wait(runtime, report.id)["status"] == "error"
    assert runtime.db.report(report.id).model_dump_json() == before


def test_stale_running_state_is_recoverable_after_process_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, report = _runtime(tmp_path)
    monkeypatch.setattr(regeneration, "_publish_locked", lambda *_args, **_kwargs: {})
    runtime.db.set_app_meta(
        "report-regeneration:" + report.id,
        f'{{"report_id": "{report.id}", "status": "running"}}',
    )
    service = AnalysisService(runtime.db, runtime.config)
    service.regenerate = MethodType(lambda self, old, **kwargs: old, service)  # type: ignore[method-assign]
    monkeypatch.setattr(runtime, "analysis", lambda **_: service)
    regeneration.start(runtime, report.id)
    assert _wait(runtime, report.id)["status"] == "success"


def test_publication_failure_restores_canonical_and_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, report = _runtime(tmp_path)
    recommendation = runtime.db.recommendations()[0]
    runtime.db.set_recommendation_feedback(recommendation["id"], "rejected", "Не менять")
    output = Path(runtime.loaded.data_dir) / runtime.config.pilot.reports_dir
    output.mkdir(parents=True, exist_ok=True)
    old_file = output / "latest.html"
    old_file.write_text("old publication", encoding="utf-8")
    before = runtime.db.report(report.id).model_dump_json()
    service = AnalysisService(runtime.db, runtime.config)
    service.regenerate = MethodType(
        lambda self, old, **kwargs: old.model_copy(update={"generated_at": datetime.now(UTC)}), service
    )  # type: ignore[method-assign]
    monkeypatch.setattr(runtime, "analysis", lambda **_: service)
    monkeypatch.setattr(
        regeneration, "_publish_locked",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk")),
    )
    regeneration.start(runtime, report.id)
    assert _wait(runtime, report.id)["status"] == "error"
    assert runtime.db.report(report.id).model_dump_json() == before
    assert old_file.read_text(encoding="utf-8") == "old publication"
    preserved = runtime.db.recommendation(recommendation["id"])
    assert preserved["status"] == "rejected"
    assert preserved["owner_note"] == "Не менять"


def test_success_preserves_owner_feedback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, report = _runtime(tmp_path)
    recommendation = runtime.db.recommendations()[0]
    runtime.db.set_recommendation_feedback(recommendation["id"], "applied", "Оставить этот режим")
    before = runtime.db.recommendation(recommendation["id"])
    monkeypatch.setattr(regeneration, "_publish_locked", lambda *_args, **_kwargs: {})
    service = AnalysisService(runtime.db, runtime.config)
    service.regenerate = MethodType(lambda self, old, **kwargs: old, service)  # type: ignore[method-assign]
    monkeypatch.setattr(runtime, "analysis", lambda **_: service)
    regeneration.start(runtime, report.id)
    assert _wait(runtime, report.id)["status"] == "success"
    after = runtime.db.recommendation(recommendation["id"])
    assert after["status"] == before["status"] == "applied"
    assert after["owner_note"] == before["owner_note"] == "Оставить этот режим"


def test_success_commits_changed_report_metrics_events_and_recommendation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, report = _runtime(tmp_path)
    recommendation = report.recommendations[0]
    extra_metric = MetricValue(id="metric:regenerated", name="fresh_metric", value=42, unit="x")
    extra_event = DetectedEvent(
        id="event:regenerated", kind="fresh_event", started_at=datetime.now(UTC), severity="info",
    )
    candidate = report.model_copy(update={
        "summary": "Свежий вывод",
        "metrics": report.metrics + [extra_metric],
        "events": report.events + [extra_event],
        "recommendations": [recommendation.model_copy(update={
            "id": recommendation.id, "suggested_manual_action": "Новый шаг",
        })],
    })
    monkeypatch.setattr(regeneration, "_publish_locked", lambda *_args, **_kwargs: {})
    service = AnalysisService(runtime.db, runtime.config)
    service.regenerate = MethodType(lambda self, old, **kwargs: candidate, service)  # type: ignore[method-assign]
    monkeypatch.setattr(runtime, "analysis", lambda **_: service)
    regeneration.start(runtime, report.id)
    assert _wait(runtime, report.id)["status"] == "success"
    saved = runtime.db.report(report.id)
    assert saved.summary == "Свежий вывод"
    assert any(item.id == "metric:regenerated" for item in saved.metrics)
    if report.events:
        assert any(item.id == "event:regenerated" for item in saved.events)
    assert runtime.db.recommendation(recommendation.id)["suggested_manual_action"] == "Новый шаг"


def test_save_report_failure_restores_files_after_override_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, report = _runtime(tmp_path)
    output = Path(runtime.loaded.data_dir) / runtime.config.pilot.reports_dir
    output.mkdir(parents=True, exist_ok=True)
    old_file = output / "latest.html"
    old_file.write_text("old publication", encoding="utf-8")
    service = AnalysisService(runtime.db, runtime.config)
    service.regenerate = MethodType(lambda self, old, **kwargs: old, service)  # type: ignore[method-assign]
    monkeypatch.setattr(runtime, "analysis", lambda **_: service)

    def publish_then_fail(*_args, **_kwargs):
        old_file.write_text("candidate publication", encoding="utf-8")

    monkeypatch.setattr(regeneration, "_publish_locked", publish_then_fail)
    original_save = runtime.db.save_report

    def fail_save(candidate, rendered):
        if candidate.id == report.id and candidate.generated_at != report.generated_at:
            raise OSError("database unavailable")
        return original_save(candidate, rendered)

    monkeypatch.setattr(runtime.db, "save_report", fail_save)
    before = runtime.db.report(report.id).model_dump_json()
    # Force a changed timestamp so the simulated failure targets the candidate.
    service.regenerate = MethodType(
        lambda self, old, **kwargs: old.model_copy(update={"generated_at": datetime.now(UTC)}), service
    )  # type: ignore[method-assign]
    regeneration.start(runtime, report.id)
    assert _wait(runtime, report.id)["status"] == "error"
    assert old_file.read_text(encoding="utf-8") == "old publication"
    assert runtime.db.report(report.id).model_dump_json() == before


def test_regeneration_api_rejects_cross_site_post(tmp_path: Path) -> None:
    runtime, report = _runtime(tmp_path)
    runtime.config.feedback.listen_port = 0
    server = build_feedback_server(runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/reports/{report.id}/regenerate"
        request = Request(url, data=b"{}", method="POST", headers={"Content-Type": "application/json", "Origin": "https://evil.example"})
        with pytest.raises(HTTPError) as error:
            urlopen(request)
        assert error.value.code == 403
        assert regeneration.status(runtime, report.id)["status"] == "idle"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_regeneration_api_passes_counterfactual_question_and_rejects_invalid_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, report = _runtime(tmp_path)
    runtime.config.feedback.listen_port = 0
    captured: list[str | None] = []

    def fake_start(_runtime, _report_id: str, question: str | None = None):
        captured.append(question)
        return {"report_id": report.id, "status": "queued", "question": question}

    monkeypatch.setattr(feedback, "start_regeneration", fake_start)
    server = build_feedback_server(runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/api/reports/{report.id}/regenerate"
    try:
        request = Request(
            url, data='{"question":"Что будет при небольшом изменении ПЗА?"}'.encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request) as response:
            assert response.status == 202
        assert captured == ["Что будет при небольшом изменении ПЗА?"]

        for body in (b'{"question":42}', b'{"question":"' + b"x" * 501 + b'"}'):
            request = Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
            with pytest.raises(HTTPError) as error:
                urlopen(request)
            assert error.value.code == 422
        assert captured == ["Что будет при небольшом изменении ПЗА?"]
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_regeneration_markup_escapes_id_and_keeps_prefixed_api_root() -> None:
    markup = render_regeneration('<script>alert(1)</script>', '/za/api')
    assert 'data-report-id="&lt;script&gt;alert(1)&lt;/script&gt;"' in markup
    assert r'\u003cscript>alert(1)\u003c/script>' in markup
    assert 'const configured = "/za/api"' in markup
    assert "location.pathname.startsWith('/za/')" in markup
    assert "api + '/reports/'" in markup


def test_counterfactual_question_is_optional_bounded_and_normalized() -> None:
    assert regeneration.normalize_counterfactual_question(None) is None
    assert regeneration.normalize_counterfactual_question("   ") is None
    assert regeneration.normalize_counterfactual_question("  Что будет при изменении ПЗА?  ") == (
        "Что будет при изменении ПЗА?"
    )
    assert regeneration.normalize_counterfactual_question("x" * 500) == "x" * 500
    with pytest.raises(ValueError, match="500"):
        regeneration.normalize_counterfactual_question("x" * 501)
    with pytest.raises(ValueError, match="строкой"):
        regeneration.normalize_counterfactual_question(42)  # type: ignore[arg-type]


def test_regeneration_markup_has_bounded_question_input_without_interpolation() -> None:
    markup = render_regeneration('<script>alert("q")</script>', "/api")
    assert 'maxlength="500"' in markup
    assert 'class="counterfactual-question"' in markup
    assert "JSON.stringify({question:text})" in markup
    assert ": '{}'" in markup
    assert 'alert("q")' not in markup
