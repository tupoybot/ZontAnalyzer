"""Artifact safety checks run without a YDB connection or network."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration.test_upload_ydb_snapshot import _artifact
from tools import transfer_ydb_snapshot as transfer
from tools.upload_ydb_snapshot import _artifact_preflight, _Pacer


def test_artifact_preflight_checks_all_rows_and_source_hash(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path / "artifact")
    manifest, digest = _artifact_preflight(artifact)
    assert manifest["tables"]["devices"]["rows"] == 2
    assert len(digest) == 64
    (artifact / "devices.jsonl").write_bytes(b"corrupt\n")
    with pytest.raises(ValueError, match="checksum"):
        _artifact_preflight(artifact)


def test_artifact_preflight_rejects_source_hash_mismatch(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path / "artifact")
    manifest = json.loads((artifact / "manifest.json").read_text())
    manifest["source_import_sha256"] = "0" * 64
    (artifact / "manifest.json").write_bytes(transfer._canonical(manifest))
    with pytest.raises(ValueError, match="backup hash mismatch"):
        _artifact_preflight(artifact)


def test_pacer_charges_more_for_indexed_batches() -> None:
    now = [0.0]
    waits = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds

    pacer = _Pacer(100, clock=lambda: now[0], sleep=sleep)
    pacer.wait([1024], False)
    pacer.wait([1024], True)
    assert waits == pytest.approx([0.11, 0.18])


def test_pacer_charges_tiny_rows_individually() -> None:
    waits = []
    pacer = _Pacer(100, clock=lambda: 0.0, sleep=waits.append)
    pacer.wait([16] * 100, True)
    assert waits == pytest.approx([8.1])


def test_bulk_pacer_limits_five_hundred_tiny_rows_at_requested_rate() -> None:
    waits = []
    pacer = _Pacer(1000, clock=lambda: 0.0, sleep=waits.append)
    pacer.wait([16] * 500, False)
    assert waits == pytest.approx([0.51])
