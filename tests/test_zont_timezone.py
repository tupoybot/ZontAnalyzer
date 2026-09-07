from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from zont_analyzer.application.ingestion import IngestionService
from zont_analyzer.application.timezone import apply_device_timezone
from zont_analyzer.config import AppConfig
from zont_analyzer.runtime import build_runtime


class FakeDb:
    def __init__(self, devices: list[dict[str, Any]]) -> None:
        self.devices = devices

    def list_devices(self) -> list[dict[str, Any]]:
        return self.devices


def resolve(raws: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    config = AppConfig.model_validate({"home": {"timezone": "America/New_York"}})
    provenance = apply_device_timezone(FakeDb([{"id": str(i), "raw": raw} for i, raw in enumerate(raws)]), config)
    return config.home.effective_timezone, provenance


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"timezone": 0}, "UTC"),
        ({"timezone": 4}, "Etc/GMT-4"),
        ({"timezone": -3}, "Etc/GMT+3"),
        ({"z3k_config": {"timezone": 4}}, "Etc/GMT-4"),
    ],
)
def test_valid_zont_offsets_are_resolved(raw: dict[str, Any], expected: str) -> None:
    timezone, provenance = resolve([raw])
    assert timezone == expected
    assert provenance["source"] == "zont"


@pytest.mark.parametrize("raw", [{}, {"timezone": True}, {"timezone": 15}, {"timezone": "4"}])
def test_missing_or_invalid_offset_uses_configured_fallback(raw: dict[str, Any]) -> None:
    timezone, provenance = resolve([raw])
    assert timezone == "America/New_York"
    assert provenance["source"] == "configuration_fallback"


def test_conflicting_device_offsets_use_configured_fallback() -> None:
    timezone, provenance = resolve([{"timezone": 4}, {"timezone": 3}])
    assert timezone == "America/New_York"
    assert provenance["reason"] == "zont_timezone_conflict"


def test_home_timezone_remains_the_configured_fallback() -> None:
    config = AppConfig()
    assert config.home.timezone == "Europe/Samara"
    assert config.home.effective_timezone == "Europe/Samara"
    assert config.home.timezone_provenance["source"] == "configuration_fallback"


def test_runtime_uses_persisted_zont_timezone_for_windows_and_reports(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("home:\n  timezone: UTC\nstorage:\n  path: state.sqlite3\n", encoding="utf-8")
    runtime = build_runtime(config_path, tmp_path / "data")
    runtime.db.save_devices([{"device_id": "1", "timezone": 4}])

    analysis = runtime.analysis(no_ai=True)
    start, end = analysis.local_day_window(date(2026, 1, 2))
    assert (start, end) == (
        datetime(2026, 1, 1, 20, tzinfo=UTC),
        datetime(2026, 1, 2, 20, tzinfo=UTC),
    )
    report = analysis.analyze_daily(date(2026, 1, 2), use_ai=False)
    assert report.timezone == "Etc/GMT-4"
    assert report.context["timezone_provenance"]["source"] == "zont"

    restarted = build_runtime(config_path, tmp_path / "data")
    assert restarted.config.home.effective_timezone == "Etc/GMT-4"


class ChangingClient:
    def __init__(self, offset: Any) -> None:
        self.offset = offset

    def discover_devices(self) -> list[dict[str, Any]]:
        return [{"device_id": "1", "timezone": self.offset}]


def test_discover_refreshes_timezone_and_invalid_data_falls_back(tmp_path: Path) -> None:
    from zont_analyzer.adapters.sqlite import Database

    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    config = AppConfig.model_validate({"home": {"timezone": "UTC"}})
    client = ChangingClient(4)
    service = IngestionService(db, client, config)
    service.discover()
    assert config.home.effective_timezone == "Etc/GMT-4"
    client.offset = 3
    service.discover()
    assert config.home.effective_timezone == "Etc/GMT-3"
    client.offset = True
    service.discover()
    assert config.home.effective_timezone == "UTC"
    config.home.timezone = "America/New_York"
    assert config.home.effective_timezone == "America/New_York"
    assert config.home.timezone_provenance["timezone"] == "America/New_York"
