#!/bin/sh
# Reproducible report, publication and gas comparison on disposable local YDB.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OUTPUT=${1:?usage: deploy/measure-ydb-cost.sh OUTPUT_DIRECTORY [GAS_BASELINE_SHA]}
GAS_BASELINE=${2:-HEAD}
IMAGE=${ZONT_TEST_IMAGE:-zont-analyzer:test-local}
YDB_IMAGE=ydbplatform/local-ydb@sha256:9e46fd45875551a75bcf34d0bb9ca0baa1d8763a4ccf2070af45f4467c4b7402
PREFIX="zont-cost-$$"
case "$OUTPUT" in /*) ;; *) echo 'output directory must be absolute' >&2; exit 2;; esac
OUTPUT=$(realpath -m -- "$OUTPUT")
case "$OUTPUT/" in "$ROOT/"*) echo 'output directory must be outside the repository' >&2; exit 2;; esac
mkdir -p "$OUTPUT/baseline" "$OUTPUT/gas-baseline"
[ -z "$(ls -A "$OUTPUT/baseline")" ] || { echo 'baseline directory must be empty' >&2; exit 2; }
GAS_BASELINE=$(git -C "$ROOT" rev-parse --verify "$GAS_BASELINE^{commit}")
for FILE in application reports publication; do
    git -C "$ROOT" show "3149665:src/zont_analyzer/adapters/ydb/$FILE.py" > "$OUTPUT/baseline/$FILE.py"
done
git -C "$ROOT" show "$GAS_BASELINE:src/zont_analyzer/application/gas.py" > "$OUTPUT/gas-baseline/gas.py"
git -C "$ROOT" show "$GAS_BASELINE:src/zont_analyzer/adapters/ydb/application.py" > "$OUTPUT/gas-baseline/application.py"
git -C "$ROOT" diff > "$OUTPUT/candidate.patch"
(cd "$ROOT"; git ls-files src tools deploy | xargs sha256sum) > "$OUTPUT/source-sha256.txt"
docker image inspect "$IMAGE" --format '{{.Id}} {{json .RepoDigests}}' > "$OUTPUT/image-identities.txt"
cleanup() {
    docker rm -f "$PREFIX-run" "$PREFIX-ready" "$PREFIX-ydb" >/dev/null 2>&1 || true
    docker network rm "$PREFIX-net" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM
docker network create --internal "$PREFIX-net" >/dev/null
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
sh "$ROOT/deploy/assert-ydb-memory.sh" "$PREFIX-ydb" "$IMAGE" > "$OUTPUT/storage-mode.json"
run() {
    docker run --rm --name "$PREFIX-run" --network "$PREFIX-net" --cpus 2 --memory 1g --memory-swap 1g \
        --mount "type=bind,src=$ROOT,dst=/workspace,readonly" \
        --mount "type=bind,src=$OUTPUT,dst=/artifacts,readonly" \
        --workdir /workspace --tmpfs /tmp:rw,nosuid,nodev,size=256m \
        -e PYTHONPATH=/workspace/src:/workspace -e YDB_ENDPOINT=grpc://ydb:2136 \
        -e YDB_DATABASE=/local -e YDB_ANONYMOUS_CREDENTIALS=1 --entrypoint python "$IMAGE" "$@"
}
for CASE in ${ZONT_COST_CASES:-reports queue gas}; do
case "$CASE" in
reports)
run tools/benchmark_report_cost.py --baseline-application /artifacts/baseline/application.py \
    --baseline-reports /artifacts/baseline/reports.py \
    --local-history-years 1 5 10 > "$OUTPUT/reports.jsonl"
;;
queue)
run tools/benchmark_queue_cost.py --baseline-publication /artifacts/baseline/publication.py \
    > "$OUTPUT/queue.jsonl"
;;
gas)
run tools/benchmark_ydb_cost.py --baseline-gas /artifacts/gas-baseline/gas.py \
    --baseline-db /artifacts/gas-baseline/application.py --baseline-commit "$GAS_BASELINE" \
    > "$OUTPUT/gas.jsonl"
;;
*) echo 'ZONT_COST_CASES must contain reports, queue or gas' >&2; exit 2;;
esac
done
docker exec "$PREFIX-ydb" sh -c 'cat /sys/fs/cgroup/cpu.stat /sys/fs/cgroup/memory.peak /sys/fs/cgroup/io.stat' \
    > "$OUTPUT/ydb-cgroup-final.txt"
echo "Results: $OUTPUT/{reports,queue,gas}.jsonl"
