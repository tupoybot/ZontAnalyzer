import json

import pytest

from tools.compact_configuration_export import compact


def _row(number, setting, device="fixture"):
    return {"device_id": device, "content_hash": str(number), "id": number, "captured_at": number,
            "payload": json.dumps({"id": device, "online": bool(number % 2),
                                   "z3k_config": {"limit": setting}})}


def test_rehearsal_keeps_recurrence_and_first_observation_without_altering_source():
    rows = [_row(4, 70), _row(1, 70), _row(3, 60), _row(2, 70), _row(1, 60, "second")]
    original = json.dumps(rows)
    retained, summary = compact(rows)
    assert [(row["device_id"], row["id"]) for row in retained] == [
        ("fixture", 1), ("fixture", 3), ("fixture", 4), ("second", 1)]
    assert json.dumps(rows) == original
    assert summary["configuration_timeline_preserved"]
    assert not summary["production_modified"]
    assert compact(retained)[0] == retained


def test_duplicate_event_identity_is_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        compact([_row(1, 70), _row(1, 60)])
