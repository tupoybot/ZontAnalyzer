#!/bin/sh
# Local-only integration checks against an isolated, disposable YDB.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
IMAGE=${ZONT_TEST_IMAGE:-zont-analyzer:test-local}
YDB_IMAGE=ydbplatform/local-ydb@sha256:9e46fd45875551a75bcf34d0bb9ca0baa1d8763a4ccf2070af45f4467c4b7402
PREFIX=${ZONT_CONTAINER_PREFIX:-zont-ydb-check-$$}
case "$PREFIX" in
    *[!a-zA-Z0-9_.-]*|'') echo "invalid ZONT_CONTAINER_PREFIX" >&2; exit 2 ;;
esac
NAME="$PREFIX-ydb"
TEST_NAME="$PREFIX-test"
METRICS_DIR=${ZONT_METRICS_DIR:-}
WORKERS=${ZONT_TEST_WORKERS:-2}
case "$WORKERS" in 1|2) ;; *) echo "ZONT_TEST_WORKERS must be 1 or 2" >&2; exit 2 ;; esac
if [ -n "$METRICS_DIR" ]; then
    case "$METRICS_DIR" in /*) ;; *) echo "ZONT_METRICS_DIR must be absolute" >&2; exit 2 ;; esac
    [ -d "$METRICS_DIR" ] && [ -w "$METRICS_DIR" ] || {
        echo "ZONT_METRICS_DIR must be an existing writable directory" >&2; exit 2;
    }
fi
stamp() {
    [ -n "$METRICS_DIR" ] || return 0
    printf '{"phase":"%s","event":"%s","epoch_ms":%s}\n' "$1" "$2" "$(date +%s%3N)" >> "$METRICS_DIR/phases.jsonl"
}
PHASE=
cleanup() {
    status=$?
    if [ "$status" -ne 0 ] && [ -n "$PHASE" ]; then
        stamp "$PHASE" failed
        if [ -n "$METRICS_DIR" ]; then printf '%s\n' "$status" > "$METRICS_DIR/exit-status.txt"; fi
    fi
    if [ -n "$METRICS_DIR" ]; then
        docker exec "$NAME" sh -c 'cat /sys/fs/cgroup/io.stat /sys/fs/cgroup/cpu.stat /sys/fs/cgroup/memory.events' \
            > "$METRICS_DIR/ydb-cgroup-final.txt" 2>/dev/null || true
    fi
    docker rm -f "$TEST_NAME" "$PREFIX-ready" "$PREFIX-cli" >/dev/null 2>&1 || true
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker network rm "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM
PHASE=preparation
stamp preparation start
docker network create "$NAME" >/dev/null
stamp preparation end
PHASE=ydb_startup
stamp ydb_startup start
docker run -d --name "$NAME" --hostname "$NAME" --network "$NAME" --memory 5g --memory-swap 5g --cpus 2 \
    -e GRPC_PORT=2136 -e MON_PORT=8765 -e YDB_USE_IN_MEMORY_PDISKS=true \
    -e YDB_DEFAULT_LOG_LEVEL=WARN "$YDB_IMAGE" >/dev/null
stamp ydb_startup end
PHASE=ydb_readiness
stamp ydb_readiness start
# Readiness is checked from the test image; no host Python or dependencies.
docker run --rm --name "$PREFIX-ready" --network "$NAME" --entrypoint python "$IMAGE" -c '
import socket,sys,time
deadline=time.monotonic()+90
while time.monotonic()<deadline:
    try:
        with socket.create_connection((sys.argv[1],2136),timeout=1): break
    except OSError: time.sleep(1)
else: raise SystemExit("YDB did not become ready")
' "$NAME"
if [ -n "$METRICS_DIR" ]; then
    sh "$ROOT/deploy/assert-ydb-memory.sh" "$NAME" "$IMAGE" > "$METRICS_DIR/storage-mode.json"
else
    sh "$ROOT/deploy/assert-ydb-memory.sh" "$NAME" "$IMAGE"
fi
stamp ydb_readiness end
PHASE=pytest
stamp pytest start
TEST_MOUNT=
if [ -n "$METRICS_DIR" ]; then
    TEST_MOUNT="type=bind,src=$METRICS_DIR,dst=/metrics"
fi
docker run --rm --name "$TEST_NAME" --network "$NAME" --cpus 2 --memory 1g --memory-swap 1g \
    --user "$(id -u):$(id -g)" --workdir /workspace \
    --mount "type=bind,src=$ROOT,dst=/workspace,readonly" \
    --tmpfs /tmp:rw,exec,nosuid,nodev,size=1g \
    ${TEST_MOUNT:+--mount "$TEST_MOUNT"} \
    ${METRICS_DIR:+-e ZONT_PYTEST_METRICS=/metrics/ydb-tests.json} \
    -e PYTHONPATH=/workspace/src:/workspace -e "YDB_TEST_ENDPOINT=grpc://$NAME:2136" \
    --entrypoint pytest "$IMAGE" -ra -p no:cacheprovider -n "$WORKERS" --dist load \
    --max-worker-restart=0 "${@:-tests}" \
    ${METRICS_DIR:+-p tools.pytest_metrics --junitxml=/metrics/ydb-junit.xml}
stamp pytest end
PHASE=cli_smoke
stamp cli_smoke start
# Exercise the installed CLI in a clean working directory and a disposable schema.
docker run --rm --name "$PREFIX-cli" --network "$NAME" --workdir /tmp \
    -e "YDB_ENDPOINT=grpc://$NAME:2136" -e YDB_DATABASE=/local \
    -e YDB_NAMESPACE=cli_smoke -e YDB_ANONYMOUS_CREDENTIALS=1 \
    --entrypoint zont-analyzer "$IMAGE" --data-dir /tmp/cli-data init
stamp cli_smoke end
PHASE=
