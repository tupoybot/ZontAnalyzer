from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zont_analyzer.adapters.openai.model_catalog import (
    CatalogSnapshot,
    ModelFact,
    parse_deprecations_html,
    parse_model_markdown,
)
from zont_analyzer.adapters.sqlite.database import Database
from zont_analyzer.application.ai_settings import AISettingsStore
from zont_analyzer.application.model_review import ModelReviewStore
from zont_analyzer.config import AppConfig, OpenAIConfig

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _fact(model: str, input_price: str | None, output_price: str | None, **changes: object) -> ModelFact:
    values: dict[str, object] = {
        "id": model, "input_price_per_mtok_usd": input_price, "output_price_per_mtok_usd": output_price,
        "reasoning_efforts": ("none", "low", "medium", "high"), "responses_supported": True,
        "structured_outputs_supported": True, "source_url": f"https://developers.openai.com/api/docs/models/{model}.md",
    }
    values.update(changes)
    return ModelFact(**values)  # type: ignore[arg-type]


class Catalog:
    def __init__(self, snapshot: CatalogSnapshot): self.snapshot, self.calls = snapshot, 0
    def fetch(self, now: datetime | None = None, model_ids: tuple[str, ...] = ()) -> CatalogSnapshot:
        self.calls += 1
        return self.snapshot


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "review.sqlite3")
    db.initialize(tmp_path / "backups")
    return db


def _settings(enabled: bool = True) -> dict[str, object]:
    return {
        "version": "settings-v1",
        "effective": {
            "review_enabled": enabled,
            "review_interval_days": 60,
            "daily_model": "gpt-5.6-terra",
            "review_model": "gpt-5.6-terra",
            "daily_reasoning_effort": "medium",
            "review_reasoning_effort": "medium",
        },
    }


def _snapshot(*models: ModelFact, incomplete: bool = False, error: str | None = None) -> CatalogSnapshot:
    return CatalogSnapshot(NOW, (), models, incomplete, error)


def test_date_first_deprecation_table_never_marks_replacement_across_rows() -> None:
    page = """| Shutdown date | Model / system | Recommended replacement |
| --- | --- | --- |
| Dec 11, 2026 | `gpt-old` | `gpt-new` |
### Next notice
| Dec 12, 2026 | `gpt-other` | `gpt-new` |"""
    assert parse_deprecations_html(page) == {"gpt-old": "Dec 11, 2026", "gpt-other": "Dec 12, 2026"}


@pytest.mark.parametrize("missing", ["current", "candidate"])
def test_unknown_price_is_unverified_not_current_optimal(tmp_path: Path, missing: str) -> None:
    current = _fact("gpt-5.6-terra", None if missing == "current" else "2", "12")
    candidate = _fact("gpt-5.6-luna", None if missing == "candidate" else "0.2", "1.2")
    store = ModelReviewStore(_db(tmp_path), Catalog(_snapshot(current, candidate)))
    result = store.run_if_due(_settings(), NOW)
    assert result and result["status"] == "unverified"
    assert store.state()["last_success_at"] is None


def test_malformed_catalog_retries_three_times_without_success(tmp_path: Path) -> None:
    catalog = Catalog(_snapshot(incomplete=True, error="official models page contained no recognised model cards"))
    store = ModelReviewStore(_db(tmp_path), catalog)
    for attempt in range(3):
        result = store.run_if_due(_settings(), NOW + timedelta(hours=6 * attempt))
        assert result and result["status"] == "unverified"
    state = store.state()
    assert state["last_success_at"] is None and state["attempts"] == 0
    assert store.run_if_due(_settings(), NOW + timedelta(hours=13)) is None


def test_disabled_manual_and_concurrent_claims_fetch_once(tmp_path: Path) -> None:
    catalog = Catalog(_snapshot(_fact("gpt-5.6-terra", "2", "12")))
    store = ModelReviewStore(_db(tmp_path), catalog)
    assert store.run_if_due(_settings(False), NOW) is None
    assert store.run_if_due(_settings(False), NOW, trigger="manual") and catalog.calls == 1
    assert store._claim(_settings(), NOW + timedelta(days=2), "manual")
    assert ModelReviewStore(store.db, catalog)._claim(_settings(), NOW + timedelta(days=2), "manual") is None


def test_restart_reclaims_expired_lease_and_marks_interruption(tmp_path: Path) -> None:
    catalog = Catalog(_snapshot(_fact("gpt-5.6-terra", "2", "12")))
    store = ModelReviewStore(_db(tmp_path), catalog)
    assert store._claim(_settings(), NOW, "manual")
    restarted = ModelReviewStore(store.db, catalog)
    assert restarted._claim(_settings(), NOW + timedelta(minutes=11), "manual")
    assert any(run["status"] == "interrupted" for run in restarted.state()["runs"])


def test_accept_real_settings_and_stale_version_guard(tmp_path: Path) -> None:
    config = AppConfig(openai=OpenAIConfig(daily_model="gpt-5.6-terra", review_model="gpt-5.6-terra"))
    db, settings = _db(tmp_path), None
    settings = AISettingsStore(db, config)
    before = settings.snapshot()
    facts = (_fact("gpt-5.6-terra", "2", "12", deprecated=True), _fact("gpt-5.6-luna", "0.2", "1.2"))
    store = ModelReviewStore(db, Catalog(_snapshot(*facts)))
    assert store.run_if_due(before, NOW)
    proposal = store.state()["proposals"][0]
    accepted = store.decide(proposal["id"], "accept", proposal["version"], settings, now=NOW)
    assert accepted["status"] == "accepted"
    field = f"{proposal['profile']}_model"
    assert settings.snapshot()["effective"][field] == "gpt-5.6-luna"
    with pytest.raises(ValueError, match="stale"):
        store.decide(proposal["id"], "accept", proposal["version"], settings, now=NOW)


def test_defer_deduplicates_notice(tmp_path: Path) -> None:
    db = _db(tmp_path)
    facts = (_fact("gpt-5.6-terra", "2", "12", deprecated=True), _fact("gpt-5.6-luna", "0.2", "1.2"))
    store = ModelReviewStore(db, Catalog(_snapshot(*facts)))
    assert store.run_if_due(_settings(), NOW)
    proposal = store.state()["proposals"][0]
    deferred = store.decide(proposal["id"], "defer", proposal["version"], AISettingsStore(db, AppConfig()), now=NOW)
    assert deferred["status"] == "deferred"
    assert store.run_if_due(_settings(), NOW + timedelta(days=61))
    matching = [item for item in store.state()["proposals"] if item["id"] == proposal["id"]]
    assert matching == [deferred]


def test_accept_rechecks_candidate_effort_before_saving(tmp_path: Path) -> None:
    db = _db(tmp_path)
    settings = AISettingsStore(db, AppConfig())
    first = _snapshot(
        _fact("gpt-5.6-terra", "2", "12"),
        _fact("gpt-5.6-luna", "0.2", "1.2"),
    )
    catalog = Catalog(first)
    store = ModelReviewStore(db, catalog)
    assert store.run_if_due(settings.snapshot(), NOW)
    proposal = store.state()["proposals"][0]
    catalog.snapshot = _snapshot(
        _fact("gpt-5.6-terra", "2", "12"),
        _fact("gpt-5.6-luna", "0.2", "1.2", reasoning_efforts=("low",)),
    )
    with pytest.raises(ValueError, match="устар|провер|глубин"):
        store.decide(proposal["id"], "accept", proposal["version"], settings, now=NOW)


def test_trimmed_official_luna_markdown_parses_default_effort_and_context_window() -> None:
    fixture = Path(__file__).parent / "fixtures" / "openai" / "luna.md"
    fact = parse_model_markdown(fixture.read_text(encoding="utf-8"), source_url="official-fixture")
    assert fact is not None
    assert fact.id == "gpt-5.6-luna"
    assert fact.reasoning_efforts == ("none", "low", "medium", "high", "xhigh", "max")
    assert fact.context_window_tokens == 1_050_000
