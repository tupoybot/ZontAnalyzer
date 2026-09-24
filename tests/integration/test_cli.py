from __future__ import annotations

import logging
from pathlib import Path

from typer.testing import CliRunner

from tests.ydb_support import make_database
from zont_analyzer.cli import app


def test_cli_registers_all_commands_and_initializes(tmp_path: Path, monkeypatch) -> None:
    root_logger = logging.getLogger()
    monkeypatch.setattr(root_logger, "handlers", list(root_logger.handlers))
    monkeypatch.setattr(root_logger, "level", root_logger.level)
    monkeypatch.chdir(tmp_path)
    db = make_database(tmp_path)
    monkeypatch.setenv("YDB_ENDPOINT", db.storage.config.endpoint)
    monkeypatch.setenv("YDB_DATABASE", db.storage.config.database)
    monkeypatch.setenv("YDB_NAMESPACE", db.storage.config.namespace)
    monkeypatch.setenv("YDB_ANONYMOUS_CREDENTIALS", "1")
    runner = CliRunner()
    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0
    assert "discover" in help_result.stdout
    result = runner.invoke(app, ["--data-dir", str(tmp_path / "data"), "init"])
    assert result.exit_code == 0
    assert "initialized" in result.stdout
    assert '"storage": "YDB"' in result.stdout
    assert '"schema_revision": "2"' in result.stdout

    analyze = runner.invoke(app, ["--data-dir", str(tmp_path / "data"), "analyze", "initial", "--no-ai"])
    assert analyze.exit_code == 0
    assert "ID отчёта:" in analyze.stdout
    export = runner.invoke(
        app,
        ["--data-dir", str(tmp_path / "data"), "report", "export", "--format", "html", "-o", "report.html"],
    )
    assert export.exit_code == 0
    assert (tmp_path / "report.html").exists()
