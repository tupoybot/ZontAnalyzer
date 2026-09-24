from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'deploy/check-legacy-image.sh'


@pytest.mark.parametrize('runtime,ancestry,allowed', [
    ('cloud', 'identical', False), ('', 'diverged', False), ('', 'behind', False),
    ('ydb-cli', 'identical', False),
    ('', 'ahead', True), ('', 'identical', True),
])
def test_legacy_gate_rejects_cloud_and_unaccepted_source(tmp_path, runtime, ancestry, allowed):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    docker = bindir / 'docker'
    docker.write_text('#!/bin/sh\ncase "$*" in *org.zont.runtime*) echo "$TEST_RUNTIME" ;; '
                      '*) echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ;; esac\n')
    curl = bindir / 'curl'
    curl.write_text('#!/bin/sh\nprintf \'{"status":"%s"}\\n\' "$TEST_ANCESTRY"\n')
    docker.chmod(0o755)
    curl.chmod(0o755)
    environment = {**os.environ, 'PATH': str(bindir) + ':' + os.environ['PATH'],
                   'TEST_RUNTIME': runtime, 'TEST_ANCESTRY': ancestry}
    result = subprocess.run(['sh', str(SCRIPT), 'ghcr.io/example/app@sha256:' + 'b' * 64],
                            env=environment, capture_output=True, text=True, timeout=5)
    assert (result.returncode == 0) is allowed
