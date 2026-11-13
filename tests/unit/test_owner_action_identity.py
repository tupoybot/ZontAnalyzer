from __future__ import annotations

from copy import deepcopy

from zont_analyzer.domain.experiments import linked_action_identity


def test_linked_action_identity_requires_exact_time_values_and_scope() -> None:
    legacy = {"recommendation_id": "rec", "experiment": {
        "category": "settings", "parameter": "curve", "before": 1.0, "after": 2,
        "performed_at": "2026-08-01T12:00:00+04:00"}}
    owner = {"recommendation_id": "rec", "experiment": {
        "category": "settings", "performed_at": "2026-08-01T08:00:00+00:00", "changes": [
            {"device_id": "device", "circuit_id": None, "parameter": "curve", "before": "1", "after": "2.0"}]}}
    expected = linked_action_identity(legacy, device_id="device")
    assert expected is not None
    assert linked_action_identity(owner) == expected
    assert linked_action_identity(legacy) is None  # Unverified device scope never supplies a match.
    for field, value in (("device_id", "other"), ("circuit_id", "circuit"), ("after", "3")):
        distinct = deepcopy(owner)
        distinct["experiment"]["changes"][0][field] = value
        assert linked_action_identity(distinct) != expected
    standalone = {**owner, "recommendation_id": None}
    assert linked_action_identity(standalone) is None


def test_linked_action_extreme_numeric_text_never_uses_decimal_context() -> None:
    item = {"recommendation_id": "rec", "experiment": {
        "category": "settings", "performed_at": "2026-08-01T08:00:00+00:00", "changes": [
            {"device_id": "device", "parameter": "curve", "before": "1e1000000", "after": "2"}]}}
    identity = linked_action_identity(item)
    equivalent = deepcopy(item)
    equivalent["experiment"]["changes"][0]["before"] = "10e999999"
    assert linked_action_identity(equivalent) == identity
