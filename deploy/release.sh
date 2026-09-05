#!/bin/sh
# Explicit deployment of a tested registry image; never builds on the server.
set -eu

image=${1:?Usage: release.sh ghcr.io/owner/image@sha256:DIGEST [env-file]}
env_file=${2:-/opt/zont-analyzer/.env}
release_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

# Require a digest, so rollback and deployment select the exact same artifact.
python3 - "$image" <<'PY'
import re
import sys
if not re.fullmatch(r'ghcr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}', sys.argv[1]):
    raise SystemExit('Pass the exact ghcr.io/...@sha256:... image reference emitted by CI')
PY

test -f "$env_file"
export ZONT_ANALYZER_IMAGE="$image"
compose() {
    docker compose --project-name zont-analyzer --env-file "$env_file" \
        -f "$release_dir/deploy/compose.yaml" -f "$release_dir/deploy/compose.test.yaml" "$@"
}
compose config --quiet
compose pull worker

# A running worker owns the live database. Use its SQLite online backup API.
# First installation is handled by OPERATIONS.md after directory preparation.
compose exec -T worker zont-analyzer --config /config/config.yaml --data-dir /data db backup

# Save the previous reference next to .env; never overwrite credentials or other settings.
python3 - "$env_file" "$image" <<'PY'
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
path = Path(sys.argv[1])
original = path.read_text()
pattern = r'^ZONT_ANALYZER_IMAGE=.*$'
entry = 'ZONT_ANALYZER_IMAGE=' + sys.argv[2]
updated = re.sub(pattern, entry, original, flags=re.MULTILINE)
if updated == original and not re.search(pattern, original, re.MULTILINE):
    updated = original.rstrip('\n') + '\n' + entry + '\n'
if updated == original:
    raise SystemExit(0)
previous = path.with_name(path.name + '.previous')
with previous.open('w') as stream:
    os.fchmod(stream.fileno(), 0o600)
    stream.write(original)
fd, temp = tempfile.mkstemp(prefix='.release-', dir=path.parent)
try:
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), stat.S_IMODE(path.stat().st_mode))
        stream.write(updated)
    os.replace(temp, path)
finally:
    if os.path.exists(temp):
        os.unlink(temp)
PY
compose up -d --no-build --wait --wait-timeout 300 worker
# Heavy doctor/integrity/analysis acceptance runs locally on an online backup copy.
# --wait already checks container health. Report it without starting another app process.
compose ps worker
