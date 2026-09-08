from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx
import pytest

from zont_analyzer.application.feedback import build_feedback_server
from zont_analyzer.runtime import build_runtime


@pytest.fixture
def ai_server(tmp_path: Path):
    runtime = build_runtime(None, tmp_path)
    runtime.config.feedback.listen_port = 0
    runtime.config.feedback.public_api_base_url = "/api"
    server = build_feedback_server(runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}/api", timeout=5) as client:
            yield runtime, client
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_ai_settings_get_put_reset_and_write_guards_are_local(ai_server, monkeypatch) -> None:
    runtime, client = ai_server
    import zont_analyzer.adapters.openai.model_catalog as catalog

    def unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("Opening settings must never fetch official pages")
    monkeypatch.setattr(catalog.OpenAIModelCatalog, "fetch", unexpected_fetch)
    initial = client.get("/ai")
    assert initial.status_code == 200
    state = initial.json()
    assert state["effective"]["daily_model"] == runtime.config.openai.daily_model
    changed = client.put("/ai", json={"expected_version": state["version"], "values": {
        "daily_model": "gpt-5.6-terra", "daily_reasoning_effort": "low",
    }})
    assert changed.status_code == 200, changed.text
    assert changed.json()["overridden"] is True
    assert runtime.analysis().config.openai.daily_model == "gpt-5.6-terra"
    reset = client.put("/ai", json={"expected_version": changed.json()["version"], "reset": True})
    assert reset.status_code == 200 and not reset.json()["overridden"]
    for response in (
        client.put("/ai", content="{}", headers={"Content-Type": "text/plain"}),
        client.put("/ai", json={"expected_version": "stale", "values": {"enabled": True}}),
        client.put("/ai", json={"expected_version": reset.json()["version"], "values": {"enabled": True}},
                   headers={"Origin": "https://elsewhere.invalid"}),
    ):
        assert response.status_code in {403, 422}


def test_manual_review_is_async_and_has_no_live_catalog_call(ai_server, monkeypatch) -> None:
    runtime, client = ai_server
    import zont_analyzer.application.ai_maintenance as maintenance

    entered, release = threading.Event(), threading.Event()
    class BlockingCatalog:
        def close(self): pass
        def fetch(self, *_args, **_kwargs):
            entered.set()
            release.wait(2)
            raise AssertionError("network must be mocked")
    monkeypatch.setattr(maintenance, "OpenAIModelCatalog", BlockingCatalog)
    response = client.put("/ai/review", json={"action": "check"})
    assert response.status_code == 202, response.text
    assert entered.wait(1)
    assert client.get("/ai").json()["review"]["running"] is True
    release.set()
    for _ in range(50):
        if not client.get("/ai").json()["review"]["running"]:
            break
        time.sleep(.02)
    assert client.get("/ai").json()["review"]["running"] is False


def test_review_decisions_require_integer_version_and_existing_proposal(ai_server) -> None:
    _, client = ai_server
    for payload in (
        {"action": "accept", "proposal_id": "missing", "expected_version": "1"},
        {"action": "reject", "proposal_id": "missing", "expected_version": 1},
        {"action": "defer", "proposal_id": 3, "expected_version": 1},
    ):
        response = client.put("/ai/review", json=payload)
        assert response.status_code in {404, 422}
