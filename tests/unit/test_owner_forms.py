import json
import re
from datetime import UTC, datetime

from zont_analyzer.domain import QualityResult, Report
from zont_analyzer.reports.owner_forms import render_owner_forms


def _report(kind: str = "daily") -> Report:
    return Report(
        id="daily-2026-09-06", kind=kind, period_start=datetime(2026, 9, 6, tzinfo=UTC),
        period_end=datetime(2026, 9, 7, tzinfo=UTC), generated_at=datetime.now(UTC),
        quality=QualityResult(
            score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
            implausible_jumps=0, sample_count=1,
        ),
        summary="ok", context={"device_id": "dev-1"},
    )


def test_owner_forms_include_safe_profile_and_daily_gas_contract() -> None:
    rendered = render_owner_forms(_report(), {"profiles": [{
        "device_id": "dev-1", "fields": {"boiler_model": {"value": '</script><b>', "source": "auto"}}, "history": [],
    }], "gas": {"reading": {"value_m3": "12.50"}}})
    assert "Профиль оборудования" in rendered
    assert "12.50" in rendered
    embedded = re.search(r'<script id="owner-initial" type="application/json">(.*?)</script>', rendered, re.S)
    assert embedded is not None and "<" not in embedded.group(1)
    assert json.loads(embedded.group(1))["profiles"][0]["fields"]["boiler_model"]["value"] == '</script><b>'
    assert "/equipment" in rendered and "/reports/" in rendered and "/gas" in rendered
    assert "has_gas_stove" in rendered and "installation_notes" in rendered
    assert 'name="gas-value"' in rendered
    assert 'name="gas-date"' not in rendered and 'name="gas-time"' not in rendered


def test_gas_form_is_absent_for_non_daily_reports() -> None:
    rendered = render_owner_forms(_report("weekly"))
    assert "data-owner-gas" not in rendered
    assert '<section class="owner-form owner-gas"' not in rendered


def test_owner_progressive_disclosures_keep_edit_and_history_hooks() -> None:
    rendered = render_owner_forms(_report(), {"gas": {"reading": {"value_m3": "12.50"}}})

    profile_start = rendered.index('<details id="system-profile"')
    assert ' open' not in rendered[profile_start:rendered.index('>', profile_start)]
    assert rendered.count('class="owner-field-group"') == 4
    assert '<div class="owner-gas-summary"><p data-gas-current>Текущее показание: 12.50 м³' in rendered
    assert 'data-gas-edit' in rendered and 'aria-expanded="false"' in rendered
    assert '<details id="gas-editor"' in rendered
    assert '<details class="owner-gas-history"' in rendered
    assert 'data-gas-save' in rendered and 'data-gas-delete' in rendered
    assert 'data-gas-plausibility' in rendered and 'data-meter-boundary' in rendered
