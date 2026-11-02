#!/bin/sh
# Local synthetic acceptance only; every case gets a fresh disposable YDB.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OUTPUT=${1:?usage: deploy/measure-gas-acceptance.sh OUTPUT_DIRECTORY [oracle|1|5|10 ...]}
shift
[ "$#" -gt 0 ] || set -- oracle 1 5 10
IMAGE=${ZONT_TEST_IMAGE:-zont-analyzer:test-local}
YDB_IMAGE=ydbplatform/local-ydb@sha256:9e46fd45875551a75bcf34d0bb9ca0baa1d8763a4ccf2070af45f4467c4b7402
PREFIX="zont-gas-acceptance-$$"
case "$OUTPUT" in /*) ;; *) echo 'output directory must be absolute' >&2; exit 2;; esac
OUTPUT=$(realpath -m -- "$OUTPUT")
case "$OUTPUT/" in "$ROOT/"*) echo 'output directory must be outside the repository' >&2; exit 2;; esac
mkdir -p "$OUTPUT"
for CASE in "$@"; do
    case "$CASE" in oracle|1|5|10) ;; *) echo 'cases must be oracle, 1, 5, or 10' >&2; exit 2;; esac
    [ ! -e "$OUTPUT/$CASE.jsonl" ] || { echo 'case output already exists' >&2; exit 2; }
done
docker image inspect "$IMAGE" --format '{{.Id}} {{json .RepoDigests}}' > "$OUTPUT/image-identity.txt"
git -C "$ROOT" rev-parse HEAD > "$OUTPUT/source-commit.txt"
(cd "$ROOT"; sha256sum tools/verify_gas_periods.py tools/benchmark_gas_updates.py \
    tools/benchmark_ydb_cost.py deploy/measure-gas-acceptance.sh \
    src/zont_analyzer/application/gas.py src/zont_analyzer/application/incremental_publication.py \
    src/zont_analyzer/adapters/ydb/period_revisions.py) > "$OUTPUT/source-sha256.txt"
cleanup_case() {
    docker rm -f "$PREFIX-run" "$PREFIX-ready" "$PREFIX-ydb" >/dev/null 2>&1 || true
}
cleanup() {
    cleanup_case
    docker network rm "$PREFIX-net" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM
docker network create --internal "$PREFIX-net" >/dev/null
for CASE in "$@"; do
    (cd "$ROOT"; sha256sum tools/verify_gas_periods.py tools/benchmark_gas_updates.py \
        deploy/measure-gas-acceptance.sh) > "$OUTPUT/$CASE-source-sha256.txt"
    docker run -d --name "$PREFIX-ydb" --hostname ydb --network "$PREFIX-net" --network-alias ydb \
        --memory 5g --memory-swap 5g --cpus 2 -e GRPC_PORT=2136 -e MON_PORT=8765 \
        -e YDB_USE_IN_MEMORY_PDISKS=true -e YDB_DEFAULT_LOG_LEVEL=WARN "$YDB_IMAGE" >/dev/null
    docker run --rm --name "$PREFIX-ready" --network "$PREFIX-net" --entrypoint python "$IMAGE" -c '
import socket,time
for attempt in range(90):
    try:
        with socket.create_connection(("ydb",2136),timeout=1): break
    except OSError: time.sleep(1)
else: raise SystemExit("local YDB did not become ready")
'
    sh "$ROOT/deploy/assert-ydb-memory.sh" "$PREFIX-ydb" "$IMAGE" > "$OUTPUT/$CASE-storage-mode.json"
    if [ "$CASE" = oracle ]; then
        MODULE=tools.verify_gas_periods
        ARGS=
    else
        MODULE=tools.benchmark_gas_updates
        ARGS="--years $CASE"
    fi
    # ARGS is built only from the validated numeric case above.
    # The memory peak below includes fixture seeding, unlike timed RPC phases.
    docker run --rm --name "$PREFIX-run" --network "$PREFIX-net" --cpus 2 --memory 1g --memory-swap 1g \
        --mount "type=bind,src=$ROOT,dst=/workspace,readonly" --workdir /workspace \
        --tmpfs /tmp:rw,nosuid,nodev,size=512m -e PYTHONPATH=/workspace/src:/workspace \
        -e YDB_ANONYMOUS_CREDENTIALS=1 --entrypoint python "$IMAGE" -m "$MODULE" $ARGS \
        > "$OUTPUT/$CASE.jsonl" 2> "$OUTPUT/$CASE.stderr"
    docker exec "$PREFIX-ydb" sh -c 'cat /sys/fs/cgroup/memory.peak' > "$OUTPUT/$CASE-ydb-memory-peak.txt"
    docker exec "$PREFIX-ydb" sh -c 'cat /sys/fs/cgroup/cpu.stat /sys/fs/cgroup/io.stat' \
        > "$OUTPUT/$CASE-ydb-resources.txt"
    cleanup_case
    echo "Completed local gas acceptance: $CASE"
done
