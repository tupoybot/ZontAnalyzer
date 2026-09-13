"""The scheduled probe must work without application dependencies or a database."""
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zont_analyzer import healthcheck


@pytest.mark.parametrize(
    ("state", "age", "expected"),
    [("ok", 0, 0), ("syncing", 0, 0), ("error", 0, 1), ("ok", 901, 1)],
)
def test_standalone_probe(tmp_path: Path, state: str, age: int, expected: int) -> None:
    path = tmp_path / "status.json"
    path.write_text(json.dumps({
        "state": state,
        "updated_at": (datetime.now(UTC) - timedelta(seconds=age)).isoformat(),
    }))
    # -S removes site-packages: importing the application/DB stack would fail.
    result = subprocess.run(
        [sys.executable, "-S", healthcheck.__file__, "--status-file", str(path)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == expected, result.stderr
    assert json.loads(result.stdout)["ok"] is (expected == 0)
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("content", [None, "{", "[]", '{"updated_at":"2026-01-01"}'])
def test_missing_invalid_status(tmp_path: Path, content: str | None) -> None:
    path = tmp_path / "status.json"
    if content is not None:
        path.write_text(content)
    assert healthcheck.worker_health(path, max_age_seconds=900)["ok"] is False
