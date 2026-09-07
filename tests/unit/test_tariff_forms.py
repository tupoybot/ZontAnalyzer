from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from zont_analyzer.adapters.sqlite.database import Database
from zont_analyzer.application.gas_tariffs import GasTariffStore
from zont_analyzer.domain import QualityResult, Report
from zont_analyzer.reports.owner_forms import render_owner_forms
from zont_analyzer.reports.owner_script import OWNER_SCRIPT


def _report(kind: str = "daily") -> Report:
    return Report(
        id="report",
        kind=kind,  # type: ignore[arg-type]
        period_start=datetime(2026, 8, 1, tzinfo=UTC),
        period_end=datetime(2026, 8, 2, tzinfo=UTC),
        generated_at=datetime(2026, 8, 2, tzinfo=UTC),
        timezone="Europe/Samara",
        quality=QualityResult(
            score=1,
            coverage_pct=100,
            max_gap_seconds=0,
            stuck_pct=0,
            implausible_jumps=0,
            sample_count=1,
        ),
        summary="ok",
        context={"device_id": "device"},
    )


def _initial(rendered: str) -> dict[str, object]:
    match = re.search(r'<script id="owner-initial" type="application/json">(.*?)</script>', rendered, re.S)
    assert match is not None
    return json.loads(match.group(1))


def test_tariff_month_defaults_to_next_calendar_month_in_report_timezone(monkeypatch) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = datetime(2026, 12, 31, 19, tzinfo=UTC)
            return moment if tz is None else moment.astimezone(tz)

    monkeypatch.setattr("zont_analyzer.reports.owner_forms.datetime", FrozenDateTime)

    rendered = render_owner_forms(_report())

    assert 'data-tariff-month type="month" value="2027-01"' in rendered
    assert "Новая цена — со следующего месяца" in rendered
    assert "formatToParts(new Date())" in OWNER_SCRIPT
    assert "tariffMonth.value =" in OWNER_SCRIPT


def test_tariff_editor_is_adjacent_to_daily_meter_and_not_in_equipment_settings() -> None:
    rendered = render_owner_forms(_report(), {"gas": {"reading": {"value_m3": "12.5"}}})

    gas_start = rendered.index('<section class="owner-form owner-gas"')
    meter_summary = rendered.index("data-gas-current", gas_start)
    tariff_summary = rendered.index("data-tariff-current", meter_summary)
    meter_editor = rendered.index('<details id="gas-editor"', tariff_summary)
    assert gas_start < meter_summary < tariff_summary < meter_editor
    assert "data-tariff-current" not in rendered[:gas_start]
    assert '<div class="owner-tariffs">' not in render_owner_forms(_report("weekly"))


def test_latest_scheduled_edit_is_the_only_active_month_value_in_embedded_data(
    tmp_path: Path, monkeypatch,
) -> None:
    db = Database(tmp_path / "tariff-form.sqlite3")
    db.initialize()
    store = GasTariffStore(db, "Europe/Samara")
    monkeypatch.setattr("zont_analyzer.application.gas_tariffs.utcnow", lambda: datetime(2026, 9, 7, tzinfo=UTC))
    first = store.save({"price": "8,01", "currency": "RUB"})
    latest = store.save({"price": "9", "currency": "RUB"})

    initial = _initial(render_owner_forms(_report(), {"tariffs": store.history()}))
    tariffs = initial["tariffs"]

    assert isinstance(tariffs, list) and len(tariffs) == 1
    assert tariffs[0]["id"] == first["id"] == latest["id"]
    assert tariffs[0]["price"] == "9"
    assert tariffs[0]["effective_month"] == "2026-10"
    assert tariffs[0]["corrections"][0]["before"]["price"] == "8.01"


def test_tariff_audit_is_script_safe_and_client_renders_it_as_text(tmp_path: Path) -> None:
    db = Database(tmp_path / "tariff-audit.sqlite3")
    db.initialize()
    store = GasTariffStore(db, "Europe/Samara")
    created = store.save({"price": "8", "currency": "RUB", "effective_month": "2026-08"})
    hostile_reason = '</script><img src=x onerror="alert(1)">'
    store.save(
        {
            "action": "correct",
            "id": created["id"],
            "price": "8.01",
            "currency": "RUB",
            "correction_reason": hostile_reason,
        }
    )

    rendered = render_owner_forms(_report(), {"tariffs": store.history()})
    embedded = re.search(r'<script id="owner-initial" type="application/json">(.*?)</script>', rendered, re.S)

    assert embedded is not None and "<" not in embedded.group(1)
    assert _initial(rendered)["tariffs"][0]["corrections"][0]["reason"] == hostile_reason
    assert "audit.textContent =" in OWNER_SCRIPT
    assert "audit.innerHTML" not in OWNER_SCRIPT
