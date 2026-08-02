from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from zont_analyzer.cli import app


class SuccessfulWorker:
    def __init__(self, _runtime: object) -> None:
        pass

    def run_cycle(self) -> dict[str, object]:
        return {"ok": True, "latest_html": "/data/reports/latest.html"}


class FailedWorker:
    def __init__(self, _runtime: object) -> None:
        pass

    def run_cycle(self) -> dict[str, object]:
        raise RuntimeError("partial sync")


def test_run_once_prints_success_and_returns_zero(tmp_path: Path, monkeypatch) -> None:
    runtime = SimpleNamespace(config=SimpleNamespace(scheduler=SimpleNamespace(sync_every_minutes=5)))
    monkeypatch.setattr("zont_analyzer.cli.build_runtime", lambda *_args: runtime)
    monkeypatch.setattr("zont_analyzer.cli.PilotService", SuccessfulWorker)

    result = CliRunner().invoke(app, ["--data-dir", str(tmp_path), "run", "--once"])

    assert result.exit_code == 0
    assert '"ok": true' in result.stdout
    assert "latest.html" in result.stdout


def test_run_once_surfaces_cycle_failure_and_returns_nonzero(tmp_path: Path, monkeypatch) -> None:
    runtime = SimpleNamespace(config=SimpleNamespace(scheduler=SimpleNamespace(sync_every_minutes=5)))
    monkeypatch.setattr("zont_analyzer.cli.build_runtime", lambda *_args: runtime)
    monkeypatch.setattr("zont_analyzer.cli.PilotService", FailedWorker)

    result = CliRunner().invoke(app, ["--data-dir", str(tmp_path), "run", "--once"])

    assert result.exit_code == 1
    assert "Worker cycle failed: RuntimeError: partial sync" in result.stderr

