from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from tests.unit.test_regulation import START, inputs
from tools.backfill_regulation import (
    FORMAT,
    _decode_inputs,
    _encode_inputs,
    _write_private,
    calculate_export,
)
from zont_analyzer.analytics.regulation import build_regulation_facts


def test_offline_backfill_preserves_facts_and_writes_private_compact_artifact(tmp_path: Path) -> None:
    from datetime import timedelta

    data = inputs()
    assert _decode_inputs(_encode_inputs(data)) == data
    source = tmp_path / "raw.json"
    output = tmp_path / "compact.json"
    _write_private(source, {"format": FORMAT, "kind": "raw_inputs", "source_identity": "synthetic-source",
                            "source_fence": {"telemetry:synthetic": 1}, "items": [
                                {"start": START.isoformat(), "end": (START + timedelta(hours=1)).isoformat(),
                                 "inputs": _encode_inputs(data)}]})
    measured = calculate_export(source, output)
    actual = json.loads(output.read_text())
    expected = build_regulation_facts(start=START, end=START + timedelta(hours=1), inputs=data)
    assert actual["items"][0]["facts"] == json.loads(json.dumps(expected))
    assert actual["source_fence"] == {"telemetry:synthetic": 1}
    assert actual["source_identity"] == "synthetic-source"
    assert measured["circuit_days"] == 1
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert not any("samples" in run for run in actual["items"][0]["facts"]["runs"])


def test_backfill_does_not_export_private_data_inside_public_checkout() -> None:
    with pytest.raises(ValueError, match="outside the checkout"):
        _write_private(Path(__file__).resolve().parents[2] / "private-regulation.json", {})
