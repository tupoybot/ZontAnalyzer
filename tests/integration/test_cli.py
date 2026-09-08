from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from zont_analyzer.cli import app


def test_cli_registers_all_commands_and_initializes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0
    assert "discover" in help_result.stdout
    result = runner.invoke(app, ["--data-dir", str(tmp_path / "data"), "init"])
    assert result.exit_code == 0
    assert "initialized" in result.stdout
    assert "d92a10b4c601" in result.stdout

    analyze = runner.invoke(app, ["--data-dir", str(tmp_path / "data"), "analyze", "initial", "--no-ai"])
    assert analyze.exit_code == 0
    assert "ID отчёта:" in analyze.stdout
    export = runner.invoke(
        app,
        ["--data-dir", str(tmp_path / "data"), "report", "export", "--format", "html", "-o", "report.html"],
    )
    assert export.exit_code == 0
    assert (tmp_path / "report.html").exists()
