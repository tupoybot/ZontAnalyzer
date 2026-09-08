from __future__ import annotations

import pytest

from zont_analyzer.reports.timezone_labels import timezone_label


def test_experiment_form_labels_timezone_without_changing_stored_identifier() -> None:
    from zont_analyzer.reports.experiment_forms import experiment_form

    form = experiment_form({}, timezone="Etc/GMT-4")
    assert "Время: UTC+4 — Самара, Удмуртия" in form
    assert 'data-timezone="Etc/GMT-4"' in form


@pytest.mark.parametrize(
    ("zone", "expected"),
    [
        ("UTC", "UTC+0 — Западноевропейское время"),
        ("Etc/GMT-1", "UTC+1 — Центральноевропейское время"),
        ("Etc/GMT-4", "UTC+4 — Самара, Удмуртия"),
        ("Etc/GMT-12", "UTC+12 — Камчатское время, Чукотка"),
    ],
)
def test_zont_fixed_offsets_use_the_application_dictionary(zone: str, expected: str) -> None:
    assert timezone_label(zone) == expected


def test_etc_gmt_uses_the_posix_opposite_sign() -> None:
    assert timezone_label("Etc/GMT+3") == "UTC-3"


@pytest.mark.parametrize("zone", ["Europe/Samara", "America/New_York", "Etc/GMT-99", "UTC+04:30", ""])
def test_unknown_or_fractional_zones_are_preserved(zone: str) -> None:
    assert timezone_label(zone) == zone


@pytest.mark.parametrize("zone", ["GMT", "Etc/UTC", "Etc/GMT", "Etc/GMT+0"])
def test_zero_offset_aliases_are_supported(zone: str) -> None:
    assert timezone_label(zone) == "UTC+0 — Западноевропейское время"
