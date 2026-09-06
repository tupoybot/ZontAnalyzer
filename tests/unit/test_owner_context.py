from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import inspect

from zont_analyzer.adapters.sqlite.database import Database, DeviceRow, RecommendationRow, ReportRow
from zont_analyzer.application.owner_context import OwnerContextStore


def _store(tmp_path: Path) -> tuple[Database, OwnerContextStore]:
    db = Database(tmp_path / "owner.sqlite3")
    db.initialize()
    with db.session() as session:
        session.add(DeviceRow(id="device", name="test", raw_json="{}"))
    return db, OwnerContextStore(db)


def _report(
    db: Database, report_id: str, *, kind: str = "daily", day: int = 0, algorithm: str = "test", device: str = "device"
) -> None:
    with db.session() as session:
        session.add(
            ReportRow(
                id=report_id,
                kind=kind,
                period_start=day,
                period_end=day + 86400,
                canonical_json=f'{{"timezone":"Europe/Samara","context":{{"device_id":"{device}"}}}}',
                generated_at=datetime.now(UTC),
                algorithm_version=algorithm,
            )
        )


def _manual(fields: dict[str, object], effective_from: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {"fields": {key: {"value": value} for key, value in fields.items()}}
    if effective_from is not None:
        payload["effective_from"] = effective_from
    return payload


def test_migration_creates_owner_tables_and_preserves_feedback(tmp_path: Path) -> None:
    db, _store_value = _store(tmp_path)
    _report(db, "r1")
    with db.session() as session:
        session.add(
            RecommendationRow(
                id="feedback",
                report_id="r1",
                category="test",
                priority="low",
                status="rejected",
                payload_json="{}",
                rejection_reason="keep this",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    db.initialize()
    assert db.recommendation("feedback") is not None
    assert {"owner_profile_revisions", "gas_readings", "gas_meter_boundaries", "gas_reading_audit"} <= set(
        inspect(db.engine).get_table_names()
    )


def test_auto_provenance_is_not_the_auto_source_and_repeated_discovery_is_idempotent(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    facts = {"coordinates": {"value": {"latitude": 48.25, "longitude": 12.5}, "source": "zont:loc"}}
    first = store.observe_auto("device", facts, "2026-01-01T00:00:00Z")
    again = store.observe_auto("device", facts, "2026-01-02T00:00:00Z")
    field = first["fields"]["coordinates"]
    assert field["source"] == "auto" and field["provenance"] == "zont:loc"
    assert len(again["history"]) == 1


def test_manual_override_survives_later_auto_discovery_until_reset(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    store.observe_auto("device", {"boiler_model": {"value": "Auto A", "source": "zont:a"}})
    store.update_profile("device", _manual({"boiler_model": "Owner model"}))
    store.observe_auto("device", {"boiler_model": {"value": "Auto B", "source": "zont:b"}})
    assert store.profile("device")["fields"]["boiler_model"]["value"] == "Owner model"


def test_reset_returns_latest_auto_and_future_auto_updates_continue(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    store.observe_auto("device", {"boiler_model": {"value": "A", "source": "zont:a"}})
    store.update_profile("device", _manual({"boiler_model": "Manual"}))
    store.observe_auto("device", {"boiler_model": {"value": "B", "source": "zont:b"}})
    reset = store.update_profile("device", {"fields": {"boiler_model": {"reset": True}}})
    assert reset["fields"]["boiler_model"]["value"] == "B"
    store.observe_auto("device", {"boiler_model": {"value": "C", "source": "zont:c"}})
    assert store.profile("device")["fields"]["boiler_model"]["value"] == "C"


def test_future_effective_manual_value_does_not_rewrite_default_history(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    store.observe_auto("device", {"auto_adapt": {"value": True, "source": "zont:auto"}}, "2026-01-01T00:00:00Z")
    store.update_profile("device", _manual({"auto_adapt": False}, "2030-01-01T00:00:00Z"))
    assert store.profile("device", "2029-12-31T23:00:00Z")["fields"]["auto_adapt"]["value"] is True
    assert store.profile("device", "2030-01-01T01:00:00Z")["fields"]["auto_adapt"]["value"] is False


def test_profile_history_and_field_timestamps_are_utc_iso(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    result = store.update_profile("device", _manual({"auto_adapt": None}, "2026-01-01T03:00:00+03:00"))
    field = result["fields"]["auto_adapt"]
    assert field["effective_from"].endswith("+00:00")
    assert result["history"][0]["recorded_at"].endswith("+00:00")


@pytest.mark.parametrize(
    "payload",
    [
        {"auto_adapt": False},
        {"fields": {"auto_adapt": False}},
        {"fields": {"auto_adapt": {"value": False, "source": "manual"}}},
        {"fields": {"missing": {"value": True}}},
    ],
)
def test_profile_payload_shape_is_strict(tmp_path: Path, payload: dict[str, object]) -> None:
    _, store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.update_profile("device", payload)


@pytest.mark.parametrize("value", ["x", 1, [], {}])
def test_tristates_reject_non_tristate_values(tmp_path: Path, value: object) -> None:
    _, store = _store(tmp_path)
    with pytest.raises(ValueError, match="auto_adapt"):
        store.update_profile("device", _manual({"auto_adapt": value}))


def test_coordinates_require_exact_object_scalars_and_geographic_bounds(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    ok = store.update_profile("device", _manual({"coordinates": {"latitude": 48.25, "longitude": 12.5}}))
    assert ok["fields"]["coordinates"]["value"]["longitude"] == 12.5
    for invalid in ({"latitude": 1}, {"latitude": "1", "longitude": 2}, {"latitude": 91, "longitude": 2}):
        with pytest.raises(ValueError):
            store.update_profile("device", _manual({"coordinates": invalid}))


def test_profile_numbers_and_gas_bounds_are_validated(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    with pytest.raises(ValueError, match="positive finite"):
        store.update_profile("device", _manual({"nominal_power_kw": float("inf")}))
    with pytest.raises(ValueError, match="must not exceed"):
        store.update_profile("device", _manual({"gas_min_m3h": 3.0, "gas_max_m3h": 2.0}))
    result = store.update_profile("device", _manual({"gas_min_m3h": 2.0, "gas_max_m3h": 3.0}))
    assert result["fields"]["gas_max_m3h"]["value"] == 3.0


def test_profile_units_source_applicability_and_dhw_enum_are_strict(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    result = store.update_profile(
        "device",
        _manual(
            {
                "gas_unit": "м³/ч",
                "gas_source": "passport page 4",
                "gas_applicability": "G20 / 24 kW",
                "dhw_type": "tank",
            }
        ),
    )
    assert result["fields"]["dhw_type"]["value"] == "tank"
    with pytest.raises(ValueError):
        store.update_profile("device", _manual({"gas_unit": " "}))
    with pytest.raises(ValueError):
        store.update_profile("device", _manual({"gas_unit": "kg/h"}))
    with pytest.raises(ValueError):
        store.update_profile("device", _manual({"dhw_type": "boiler"}))


def test_empty_profile_update_is_an_optional_noop_and_null_is_explicit_unknown(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    assert store.update_profile("device", {"fields": {}})["history"] == []
    result = store.update_profile("device", _manual({"coordinates": None, "nominal_power_kw": None}))
    assert result["fields"]["coordinates"]["value"] is None
    with pytest.raises(KeyError):
        store.profile("missing")


def test_concurrent_writes_are_serialized_with_begin_immediate(tmp_path: Path) -> None:
    _, store = _store(tmp_path)

    def write() -> bool:
        return store.update_profile("device", _manual({"auto_adapt": False}))["fields"]["auto_adapt"]["value"] is False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _item: write(), range(2))) == [True, True]
    assert len(store.profile("device")["history"]) == 1


def test_gas_is_daily_and_day_is_server_authoritative(tmp_path: Path) -> None:
    db, store = _store(tmp_path)
    _report(db, "weekly", kind="weekly")
    with pytest.raises(ValueError, match="daily"):
        store.update_gas("weekly", {"value_m3": "1"})
    with pytest.raises(KeyError):
        store.update_gas("missing", {"value_m3": "1"})


def test_gas_aliases_for_same_daily_day_share_one_reading(tmp_path: Path) -> None:
    db, store = _store(tmp_path)
    _report(db, "r1", day=86400 * 20)
    _report(db, "alias", day=86400 * 20, algorithm="test-alias")
    store.update_gas("r1", {"value_m3": "10.250"})
    alias = store.gas("alias")
    assert alias["reading"] is not None and alias["reading"]["value_m3"] == "10.25"
    assert alias["time_precision"] == "day"


def test_gas_state_is_idempotent_without_an_idempotency_key(tmp_path: Path) -> None:
    db, store = _store(tmp_path)
    _report(db, "r1", day=86400 * 20)
    first = store.update_gas("r1", {"value_m3": "10.25"})
    again = store.update_gas("r1", {"value_m3": "10.250"})
    assert first["reading"] == again["reading"]
    assert len(again["audit"]) == 1


def test_gas_payload_is_strict_and_decimal_exponents_are_bounded(tmp_path: Path) -> None:
    db, store = _store(tmp_path)
    _report(db, "r1", day=86400 * 20)
    for payload in ({"value": 1}, {"value_m3": 1, "idempotency_key": "x"}, {"delete": "yes"}):
        with pytest.raises(ValueError):
            store.update_gas("r1", payload)
    for value in ("1e999999999", "1e-999999999", "0.0000001"):
        with pytest.raises(ValueError):
            store.update_gas("r1", {"value_m3": value})


def test_gas_monotonicity_is_checked_inside_server_meter_segment(tmp_path: Path) -> None:
    db, store = _store(tmp_path)
    _report(db, "r1", day=86400 * 20)
    _report(db, "r2", day=86400 * 21)
    store.update_gas("r1", {"value_m3": 10})
    with pytest.raises(ValueError, match="Конфликт.*1970-01-21"):
        store.update_gas("r2", {"value_m3": 9})
    store.update_gas("r2", {"value_m3": 11})


def test_reset_boundary_persists_and_applies_to_out_of_order_days(tmp_path: Path) -> None:
    db, store = _store(tmp_path)
    _report(db, "r1", day=86400 * 20)
    _report(db, "r2", day=86400 * 21)
    _report(db, "r3", day=86400 * 22)
    reset = store.update_gas("r2", {"value_m3": 2, "reset": True})
    store.update_gas("r3", {"value_m3": 7})
    store.update_gas("r1", {"value_m3": 100})
    assert reset["reading"] is not None and reset["reading"]["meter_segment"].startswith("reset:")
    assert store.gas("r3")["reading"]["meter_segment"] == reset["reading"]["meter_segment"]


def test_correction_does_not_erase_existing_reset_boundary(tmp_path: Path) -> None:
    db, store = _store(tmp_path)
    _report(db, "r1", day=86400 * 20)
    _report(db, "r2", day=86400 * 21)
    store.update_gas("r1", {"value_m3": 2, "reset": True})
    corrected = store.update_gas("r1", {"value_m3": 3})
    store.update_gas("r2", {"value_m3": 4})
    assert corrected["reading"] is not None and corrected["reading"]["meter_segment"].startswith("reset:")


def test_delete_preserves_reset_boundary_and_returns_deleted_audit(tmp_path: Path) -> None:
    db, store = _store(tmp_path)
    _report(db, "r1", day=86400 * 20)
    _report(db, "r2", day=86400 * 21)
    store.update_gas("r1", {"value_m3": 2, "reset": True})
    deleted = store.update_gas("r1", {"delete": True})
    store.update_gas("r2", {"value_m3": 4})
    assert deleted["reading"] is None and deleted["audit"][-1]["action"] == "delete"
    assert store.gas("r2")["reading"]["meter_segment"].startswith("reset:")


def test_gas_plausibility_is_explicitly_unknown_without_profile_data(tmp_path: Path) -> None:
    db, store = _store(tmp_path)
    _report(db, "r1", day=86400 * 20)
    result = store.update_gas("r1", {"value_m3": 12})
    assert result["plausibility"]["status"] == "unknown"
    assert "время горения" not in result["plausibility"]["reason"]


def test_gas_plausibility_warns_only_against_historical_known_maximum(tmp_path: Path) -> None:
    db, store = _store(tmp_path)
    _report(db, "r1", day=86400 * 20)
    _report(db, "r2", day=86400 * 21)
    store.update_profile(
        "device",
        _manual(
            {
                "gas_max_m3h": 1.0,
                "has_gas_stove": False,
            },
            "1970-01-01T00:00:00Z",
        ),
    )
    store.update_gas("r1", {"value_m3": 1})
    result = store.update_gas("r2", {"value_m3": 100})
    assert result["plausibility"]["status"] == "warning"
    assert result["plausibility"]["warnings"]
