from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]
RELEASE_SCRIPT = ROOT / "deploy" / "release.sh"
DIGEST = f"ghcr.io/tupoybot/zontanalyzer@sha256:{'a' * 64}"


def _fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "docker.log"
    executable = tmp_path / "docker"
    executable.write_text(
        """#!/bin/sh
set -eu
printf '%s\\n' \"$*\" >> \"$FAKE_DOCKER_LOG\"
case \"${FAKE_DOCKER_FAIL_ON:-}\" in
  pull) case \"$*\" in *\" pull worker\") exit 31;; esac ;;
  backup) case \"$*\" in *\" db backup\") exit 32;; esac ;;
esac
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable, log


def _run_release(
    tmp_path: Path, env_file: Path, image: str = DIGEST, *, fail_on: str = ""
) -> subprocess.CompletedProcess[str]:
    fake_docker, log = _fake_docker(tmp_path)
    environment = os.environ | {
        "PATH": f"{fake_docker.parent}:{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_DOCKER_FAIL_ON": fail_on,
    }
    return subprocess.run(
        [str(RELEASE_SCRIPT), image, str(env_file)],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _commands(tmp_path: Path) -> list[str]:
    log = tmp_path / "docker.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_release_rejects_invalid_digest_before_docker_or_env_changes(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    original = "ZONT_ANALYZER_IMAGE=old\nZONT_CLIENT_TOKEN=secret\n"
    env_file.write_text(original, encoding="utf-8")

    result = _run_release(tmp_path, env_file, "ghcr.io/tupoybot/zontanalyzer:latest")

    assert result.returncode != 0
    assert "Pass the exact ghcr.io" in result.stderr
    assert env_file.read_text(encoding="utf-8") == original
    assert not env_file.with_name(".env.previous").exists()
    assert _commands(tmp_path) == []


def test_release_pull_failure_leaves_env_untouched_without_backup_or_up(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    original = "ZONT_ANALYZER_IMAGE=old\nKEEP=this\n"
    env_file.write_text(original, encoding="utf-8")

    result = _run_release(tmp_path, env_file, fail_on="pull")

    assert result.returncode == 31
    assert env_file.read_text(encoding="utf-8") == original
    assert not env_file.with_name(".env.previous").exists()
    commands = _commands(tmp_path)
    assert any(command.endswith(" config --quiet") for command in commands)
    assert any(command.endswith(" pull worker") for command in commands)
    assert not any(" db backup" in command or " up -d " in command for command in commands)


def test_release_backup_failure_leaves_env_untouched_without_up(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    original = "ZONT_ANALYZER_IMAGE=old\nKEEP=this\n"
    env_file.write_text(original, encoding="utf-8")

    result = _run_release(tmp_path, env_file, fail_on="backup")

    assert result.returncode == 32
    assert env_file.read_text(encoding="utf-8") == original
    assert not env_file.with_name(".env.previous").exists()
    commands = _commands(tmp_path)
    assert any(command.endswith(" pull worker") for command in commands)
    assert any(" db backup" in command for command in commands)
    assert not any(" up -d " in command for command in commands)


def test_release_pulls_backs_up_checks_then_updates_only_image_and_healthchecks(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    original = "ZONT_CLIENT_TOKEN=secret\nZONT_ANALYZER_IMAGE=old\nUNRELATED_SETTING=preserved\n"
    env_file.write_text(original, encoding="utf-8")

    result = _run_release(tmp_path, env_file)

    assert result.returncode == 0, result.stderr
    assert env_file.read_text(encoding="utf-8") == (
        "ZONT_CLIENT_TOKEN=secret\n"
        f"ZONT_ANALYZER_IMAGE={DIGEST}\n"
        "UNRELATED_SETTING=preserved\n"
    )
    assert env_file.with_name(".env.previous").read_text(encoding="utf-8") == original
    commands = _commands(tmp_path)
    stages = [
        next(index for index, command in enumerate(commands) if command.endswith(" pull worker")),
        next(index for index, command in enumerate(commands) if " db backup" in command),
        next(index for index, command in enumerate(commands) if " doctor" in command),
        next(
            index
            for index, command in enumerate(commands)
            if " up -d --no-build --wait --wait-timeout 300 worker" in command
        ),
        next(index for index, command in enumerate(commands) if " healthcheck" in command),
    ]
    assert stages == sorted(stages)
    assert sum(" doctor" in command for command in commands) == 2


def test_retry_keeps_the_previous_release_reference(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    original = "ZONT_ANALYZER_IMAGE=old\nKEEP=this\n"
    env_file.write_text(original)
    assert _run_release(tmp_path, env_file).returncode == 0
    assert _run_release(tmp_path, env_file).returncode == 0
    assert env_file.with_name(".env.previous").read_text() == original
