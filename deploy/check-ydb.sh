#!/bin/sh
# Local-only integration checks against an isolated, disposable YDB.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
IMAGE=${ZONT_TEST_IMAGE:-zont-analyzer:test-local}
YDB_IMAGE=ydbplatform/local-ydb@sha256:9e46fd45875551a75bcf34d0bb9ca0baa1d8763a4ccf2070af45f4467c4b7402
NAME="zont-ydb-check-$$"
cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker network rm "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM
docker network create "$NAME" >/dev/null
docker run -d --name "$NAME" --hostname "$NAME" --network "$NAME" --memory 5g \
    -e GRPC_PORT=2136 -e MON_PORT=8765 -e YDB_USE_IN_MEMORY_PDISKS=1 \
    -e YDB_DEFAULT_LOG_LEVEL=WARN "$YDB_IMAGE" >/dev/null
# Readiness is checked from the test image; no host Python or dependencies.
docker run --rm --network "$NAME" --entrypoint python "$IMAGE" -c '
import socket,sys,time
deadline=time.monotonic()+90
while time.monotonic()<deadline:
    try:
        with socket.create_connection((sys.argv[1],2136),timeout=1): break
    except OSError: time.sleep(1)
else: raise SystemExit("YDB did not become ready")
' "$NAME"
docker run --rm --network "$NAME" --user "$(id -u):$(id -g)" --workdir /workspace \
    --mount "type=bind,src=$ROOT,dst=/workspace,readonly" \
    --tmpfs /tmp:rw,exec,nosuid,nodev,size=1g \
    -e PYTHONPATH=/workspace/src -e "YDB_TEST_ENDPOINT=grpc://$NAME:2136" \
    --entrypoint sh "$IMAGE" -c 'exec pytest -ra -p no:cacheprovider tests/integration/test_ydb_*.py'
