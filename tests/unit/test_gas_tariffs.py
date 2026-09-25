from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.ydb_support import make_database
from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.application.gas_tariffs import CURRENCIES, GasTariffStore


def _store(tmp_path: Path, timezone: str = "Europe/Moscow") -> tuple[Database, GasTariffStore]:
    db = make_database(tmp_path)
    return db, GasTariffStore(db, timezone)


@pytest.mark.ydb
def test_create_accepts_comma_decimal_and_month_start_uses_local_timezone(tmp_path: Path) -> None:
    _, store = _store(tmp_path)

    result = store.save({"price": "8,01", "currency": "rub", "effective_month": "2026-10"})

    assert result["action"] == "create"
    assert result["idempotent"] is False
    assert result["affected_start"] == "2026-09-30T21:00:00+00:00"
    assert result["affected_end"] is None
    assert result["tariff"]["price"] == "8.01"
    assert result["tariff"]["currency"] == "RUB"
    assert result["tariff"]["effective_month"] == "2026-10"
    assert result["tariff"]["recorded_at"].endswith("+00:00")
    assert result["tariff"]["corrections"] == []


@pytest.mark.ydb
def test_omitted_month_uses_first_day_of_next_local_month(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, store = _store(tmp_path, "Asia/Almaty")
    monkeypatch.setattr("zont_analyzer.application.gas_tariffs.utcnow", lambda: datetime(2026, 12, 31, 19, tzinfo=UTC))

    result = store.save({"price": 9, "currency": "KZT"})

    assert result["tariff"]["effective_month"] == "2027-02"
    assert result["affected_start"] == "2027-01-31T19:00:00+00:00"


@pytest.mark.ydb
def test_history_is_chronological_and_affected_range_ends_at_next_tariff(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    later = store.save({"price": "9", "currency": "RUB", "effective_month": "2026-11"})
    earlier = store.save({"price": "8.01", "currency": "RUB", "effective_month": "2026-10"})

    history = store.history()

    assert [item["effective_month"] for item in history] == ["2026-10", "2026-11"]
    assert earlier["affected_end"] == later["affected_start"]
    assert later["affected_end"] is None


@pytest.mark.ydb
def test_changed_historical_month_requires_explicit_correction_and_audits_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store = _store(tmp_path)
    monkeypatch.setattr("zont_analyzer.application.gas_tariffs.utcnow", lambda: datetime(2026, 9, 7, tzinfo=UTC))
    created = store.save({"price": "8.01", "currency": "RUB", "effective_month": "2026-08"})
    with pytest.raises(ValueError, match="explicit correction"):
        store.save({"price": "9", "currency": "RUB", "effective_month": "2026-08"})

    corrected = store.save(
        {
            "action": "correct",
            "id": created["id"],
            "price": "9",
            "currency": "USD",
            "correction_reason": "Исправление квитанции",
        }
    )

    assert corrected["id"] == created["id"]
    assert corrected["tariff"]["effective_month"] == "2026-08"
    assert corrected["tariff"]["price"] == "9"
    assert corrected["tariff"]["currency"] == "USD"
    assert corrected["tariff"]["recorded_at"] == created["tariff"]["recorded_at"]
    correction = corrected["tariff"]["corrections"][0]
    assert correction["before"] == {"price": "8.01", "currency": "RUB"}
    assert correction["after"] == {"price": "9", "currency": "USD"}
    assert correction["reason"] == "Исправление квитанции"


@pytest.mark.ydb
def test_last_normal_save_wins_for_same_planned_month_and_is_audited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store = _store(tmp_path)
    monkeypatch.setattr("zont_analyzer.application.gas_tariffs.utcnow", lambda: datetime(2026, 9, 7, tzinfo=UTC))
    first = store.save({"price": "8.01", "currency": "RUB"})

    latest = store.save({"price": "9", "currency": "RUB"})

    assert latest["id"] == first["id"]
    assert latest["action"] == "correct"
    assert latest["tariff"]["price"] == "9"
    assert latest["tariff"]["corrections"][0]["before"]["price"] == "8.01"
    assert latest["tariff"]["corrections"][0]["reason"] is None


@pytest.mark.ydb
def test_exact_repeated_create_and_correction_are_idempotent(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    payload = {"price": "8.010", "currency": "RUB", "effective_month": "2026-10"}
    first = store.save(payload)
    again = store.save(payload)
    correction = {
        "action": "correct",
        "id": first["id"],
        "price": "9",
        "currency": "RUB",
        "correction_reason": "Опечатка",
    }
    corrected = store.save(correction)
    repeated = store.save(correction)

    assert again["id"] == first["id"] and again["idempotent"] is True
    assert corrected["idempotent"] is False and repeated["idempotent"] is True
    assert len(store.history()[0]["corrections"]) == 1


@pytest.mark.ydb
def test_latest_of_multiple_explicit_corrections_is_active(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    created = store.save({"price": "8", "currency": "RUB", "effective_month": "2026-08"})
    for price, reason in (("8.5", "Первая проверка"), ("8.75", "Последняя квитанция")):
        store.save(
            {
                "action": "correct",
                "id": created["id"],
                "price": price,
                "currency": "RUB",
                "correction_reason": reason,
            }
        )

    active = store.history()[0]
    assert active["price"] == "8.75"
    assert [item["after"]["price"] for item in active["corrections"]] == ["8.5", "8.75"]


@pytest.mark.ydb
def test_price_validation_rejects_unsafe_values(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    for price in (None, True, "", "nan", "inf", "-0.01", "1e999999", "0.0000001"):
        with pytest.raises(ValueError, match="price"):
            store.save({"price": price, "currency": "RUB", "effective_month": "2026-10"})


@pytest.mark.ydb
def test_only_supported_currencies_are_accepted(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    for currency in (None, "", "BTC", 1):
        with pytest.raises(ValueError, match="currency"):
            store.save({"price": 0, "currency": currency, "effective_month": "2026-10"})
    assert CURRENCIES == ("RUB", "USD", "EUR", "GBP", "KZT", "BYN")


@pytest.mark.ydb
def test_effective_month_is_strict(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    for month in ("2026-1", "2026-13", "01-2026", "2026-10-01", 202610):
        with pytest.raises(ValueError, match="effective_month"):
            store.save({"price": 8, "currency": "RUB", "effective_month": month})


@pytest.mark.ydb
def test_effective_month_timezone_conversion_overflow_is_a_validation_error(tmp_path: Path) -> None:
    _, store = _store(tmp_path, "Etc/GMT-14")
    with pytest.raises(ValueError, match="timezone range"):
        store.save({"price": 8, "currency": "RUB", "effective_month": "0001-01"})


@pytest.mark.ydb
def test_correction_requires_reason_and_cannot_move_month(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    created = store.save({"price": 8, "currency": "RUB", "effective_month": "2026-10"})
    with pytest.raises(ValueError, match="correction_reason"):
        store.save({"action": "correct", "id": created["id"], "price": 9, "currency": "RUB"})
    with pytest.raises(ValueError, match="unsupported"):
        store.save(
            {
                "action": "correct",
                "id": created["id"],
                "price": 9,
                "currency": "RUB",
                "effective_month": "2026-11",
                "correction_reason": "move",
            }
        )


@pytest.mark.ydb
def test_scopes_are_isolated_and_correction_cannot_cross_scope(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    created = store.save({"price": 8, "currency": "RUB", "effective_month": "2026-10"})
    store.save({"price": 10, "currency": "EUR", "effective_month": "2026-10"}, scope="meter:2")

    assert len(store.history()) == 1
    assert store.history("meter:2")[0]["currency"] == "EUR"
    with pytest.raises(KeyError):
        store.save(
            {
                "action": "correct",
                "id": created["id"],
                "price": 9,
                "currency": "RUB",
                "correction_reason": "wrong scope",
            },
            scope="meter:2",
        )


@pytest.mark.ydb
def test_invalid_payload_timezone_and_missing_id_are_rejected(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    with pytest.raises(ValueError, match="object"):
        store.save([])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timezone"):
        store.save({"price": 8, "currency": "RUB"}, timezone="Missing/Zone")
    with pytest.raises(ValueError, match="id"):
        store.save({"action": "correct", "price": 9, "currency": "RUB", "correction_reason": "typo"})
