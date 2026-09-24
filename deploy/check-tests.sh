#!/bin/sh
# Full suite: pure logic without a network, then real YDB integration tests.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
IMAGE=${ZONT_TEST_IMAGE:-zont-analyzer:test-local}
PREFIX=${ZONT_CONTAINER_PREFIX:-zont-check-$$}
case "$PREFIX" in
    *[!a-zA-Z0-9_.-]*|'') echo "invalid ZONT_CONTAINER_PREFIX" >&2; exit 2 ;;
esac
METRICS_DIR=${ZONT_METRICS_DIR:-}
[ "$#" -gt 0 ] || set -- tests
stamp() {
    [ -n "$METRICS_DIR" ] || return 0
    printf '{"phase":"pure_tests","event":"%s","epoch_ms":%s}\n' "$1" "$(date +%s%3N)" >> "$METRICS_DIR/phases.jsonl"
}
cleanup() {
    docker rm -f "$PREFIX-pure" "$PREFIX-test" "$PREFIX-ready" "$PREFIX-cli" "$PREFIX-ydb" \
        >/dev/null 2>&1 || true
    docker network rm "$PREFIX-ydb" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM
TEST_MOUNT=
if [ -n "$METRICS_DIR" ]; then
    case "$METRICS_DIR" in /*) ;; *) echo "ZONT_METRICS_DIR must be absolute" >&2; exit 2 ;; esac
    [ -d "$METRICS_DIR" ] && [ -w "$METRICS_DIR" ] || exit 2
    TEST_MOUNT="type=bind,src=$METRICS_DIR,dst=/metrics"
fi
stamp start
docker run --rm --name "$PREFIX-pure" --network none \
    --cpus 2 --memory 1g --memory-swap 1g \
    --user "$(id -u):$(id -g)" --workdir /workspace \
    --mount "type=bind,src=$ROOT,dst=/workspace,readonly" \
    --tmpfs /tmp:rw,exec,nosuid,nodev,size=1g \
    ${TEST_MOUNT:+--mount "$TEST_MOUNT"} \
    ${METRICS_DIR:+-e ZONT_PYTEST_METRICS=/metrics/pure-tests.json} \
    -e PYTHONPATH=/workspace/src:/workspace \
    --entrypoint pytest "$IMAGE" -ra -p no:cacheprovider -m 'not ydb' "$@" \
    ${METRICS_DIR:+-p tools.pytest_metrics --junitxml=/metrics/pure-junit.xml}
stamp end
ZONT_CONTAINER_PREFIX="$PREFIX" "$ROOT/deploy/check-ydb.sh" -m ydb "$@"
