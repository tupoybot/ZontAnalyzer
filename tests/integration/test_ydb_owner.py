from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from zont_analyzer.adapters.ydb.owner import OwnerRepository


@pytest.mark.ydb
def test_gas_reading_move_is_unique_audited_and_compare_and_set(ydb_database: object) -> None:
    repo = OwnerRepository(ydb_database)  # type: ignore[arg-type]
    first = repo.save_gas_reading(
        "device",
        "2026-09-01",
        "12.3400",
        payload={"id": "legacy-gas-1", "source": "manual", "report_id": "report-1", "meter_segment": "meter-1"},
    )
    assert first["value_m3"] == "12.3400"
    assert first["version"] == 1
    moved = repo.move_gas_reading("device", "2026-09-01", "2026-09-02", expected_version=1)
    assert moved["value_m3"] == "12.3400"
    assert repo.gas_reading("device", "2026-09-01") is None
    persisted = repo.gas_reading("device", "2026-09-02")
    assert persisted is not None and persisted["version"] == 2
    assert {key: persisted[key] for key in ("id", "source", "report_id", "meter_segment")} == {
        "id": "legacy-gas-1",
        "source": "manual",
        "report_id": "report-1",
        "meter_segment": "meter-1",
    }
    occupied = repo.save_gas_reading("device", "2026-09-03", "30")
    before_audit = repo.gas_audit_history("device")
    with pytest.raises(ValueError, match="destination occupied"):
        repo.move_gas_reading("device", "2026-09-02", "2026-09-03", expected_version=2)
    assert repo.gas_reading("device", "2026-09-02") == persisted
    assert repo.gas_reading("device", "2026-09-03") == occupied
    assert repo.gas_audit_history("device") == before_audit
    with pytest.raises(ValueError, match="changed"):
        repo.save_gas_reading("device", "2026-09-02", "13", expected_version=1)
    repo.save_gas_reading("device", "2026-09-04", "40")
    first_page = repo.gas_audit_history("device", limit=2)
    second_page = repo.gas_audit_history("device", limit=2, after_id=first_page[-1]["audit_id"])
    assert len(first_page) == 2 and len(second_page) == 2
    assert first_page[-1]["audit_id"] < second_page[0]["audit_id"]
    last_page = repo.gas_audit_history("device", limit=2, after_id=second_page[-1]["audit_id"])
    assert len(last_page) == 1
    assert repo.gas_audit_history("device", limit=2, after_id=last_page[-1]["audit_id"]) == []


@pytest.mark.ydb
def test_profile_keeps_same_time_revisions_and_as_of_view(ydb_database: object) -> None:
    repo = OwnerRepository(ydb_database)  # type: ignore[arg-type]
    moment = datetime(2026, 9, 1, tzinfo=UTC)
    assert (
        repo.save_profile_revision(
            "device",
            "city",
            "A",
            effective_at=moment,
            provenance="manual",
            expected_version=0,
            metadata={"id": "profile-rev-1", "source": "legacy-import"},
        )
        == 1
    )
    assert (
        repo.save_profile_revision("device", "city", "B", effective_at=moment, provenance="manual", expected_version=1)
        == 2
    )
    assert (
        repo.save_profile_revision("device", "city", "C", effective_at=moment, provenance="manual", expected_version=2)
        == 3
    )
    first_page = repo.profile_history("device", "city", limit=2)
    second_page = repo.profile_history("device", "city", limit=2, after_revision=first_page[-1]["revision"])
    history = first_page + second_page
    assert [entry["value"] for entry in history] == ["A", "B", "C"]
    assert history[0]["id"] == "profile-rev-1"
    assert history[0]["source"] == "legacy-import"
    assert repo.profile("device", as_of=moment)["city"]["value"] == "C"
    with pytest.raises(ValueError, match="changed"):
        repo.save_profile_revision(
            "device", "city", "stale", effective_at=moment, provenance="manual", expected_version=1
        )


@pytest.mark.ydb
def test_tariff_exact_decimal_history_and_validation(ydb_database: object) -> None:
    repo = OwnerRepository(ydb_database)  # type: ignore[arg-type]
    first = repo.save_tariff(
        "home", "2026-09", "0.123400", "rub", expected_version=0, metadata={"id": "tariff-1", "source": "manual"}
    )
    second = repo.save_tariff("home", "2026-09", "0.125", "RUB", expected_version=1)
    assert first["price"] == "0.123400"
    assert second["price"] == "0.125"
    assert second["currency"] == "RUB"
    assert len(repo.tariff_history("home")) == 1
    repo.save_tariff("home", "2026-10", "0.2", "RUB", expected_version=0)
    repo.save_tariff("home", "2026-11", "0.3", "RUB", expected_version=0)
    tariff_first = repo.tariff_history("home", limit=2)
    tariff_second = repo.tariff_history("home", limit=2, after_month=tariff_first[-1]["effective_month"])
    assert [item["effective_month"] for item in tariff_first + tariff_second] == ["2026-09", "2026-10", "2026-11"]
    audit_first = repo.tariff_audit_history("home", limit=2)
    audit_second = repo.tariff_audit_history("home", limit=2, after_id=audit_first[-1]["audit_id"])
    audit = audit_first + audit_second
    assert len(audit) == 4
    assert audit[0]["before"] is None
    assert audit[1]["before"]["price"] == "0.123400"
    with pytest.raises(ValueError):
        repo.save_tariff("home", "2026-13", "1", "RUB")
    with pytest.raises(ValueError):
        repo.save_tariff("home", "2026-09", "NaN", "RUB")


@pytest.mark.ydb
def test_model_proposal_decision_and_settings_version_are_atomic(ydb_database: object) -> None:
    repo = OwnerRepository(ydb_database)  # type: ignore[arg-type]
    assert repo.save_ai_settings({"model": "old"}, expected_version=0) == 1
    repo.create_model_proposal("proposal", {"model": "new"}, model="candidate", base_settings_version=1)
    with pytest.raises(ValueError, match="changed"):
        repo.decide_model_proposal("proposal", decision="accepted", expected_settings_version=0)
    assert repo.ai_settings()["settings"] == {"model": "old"}  # type: ignore[index]
    assert repo.decide_model_proposal("proposal", decision="accepted", expected_settings_version=1) == 2
    repo.create_model_proposal("proposal", {"model": "new"}, model="candidate", base_settings_version=1)
    assert repo.ai_settings()["settings"] == {"model": "new"}  # type: ignore[index]
    with pytest.raises(ValueError, match="already decided"):
        repo.decide_model_proposal("proposal", decision="rejected", expected_settings_version=2)


@pytest.mark.ydb
def test_meter_boundary_has_versioned_audit_and_decimal_rejects_negative(ydb_database: object) -> None:
    repo = OwnerRepository(ydb_database)  # type: ignore[arg-type]
    boundary = repo.save_meter_boundary("device", "2026-09-01", meter_id="meter-a", expected_version=0)
    assert boundary["version"] == 1
    assert repo.save_meter_boundary("device", "2026-09-01", meter_id="meter-b", expected_version=1)["version"] == 2
    with pytest.raises(ValueError, match="negative"):
        repo.save_gas_reading("device", "2026-09-03", "-0.1")


@pytest.mark.ydb
def test_competing_gas_updates_accept_only_one_compare_and_set(ydb_database: object) -> None:
    repo = OwnerRepository(ydb_database)  # type: ignore[arg-type]
    repo.save_gas_reading("device", "2026-09-01", "10")

    def update(value: str) -> bool:
        try:
            repo.save_gas_reading("device", "2026-09-01", value, expected_version=1)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(update, ("11", "12")))
    assert sorted(results) == [False, True]
    assert repo.gas_reading("device", "2026-09-01")["version"] == 2  # type: ignore[index]
