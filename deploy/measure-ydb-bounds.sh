#!/bin/sh
# Isolated local Query API comparison for issue #65. No production credentials.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BASELINE=${1:?usage: deploy/measure-ydb-bounds.sh BASELINE_SHA OUTPUT_DIRECTORY}
OUTPUT=${2:?output directory is required}
IMAGE=${ZONT_TEST_IMAGE:-zont-analyzer:test-local}
YDB_IMAGE=ydbplatform/local-ydb@sha256:9e46fd45875551a75bcf34d0bb9ca0baa1d8763a4ccf2070af45f4467c4b7402
PREFIX="zont-bounds-$$"
case "$OUTPUT" in /*) ;; *) echo 'output directory must be absolute' >&2; exit 2;; esac
OUTPUT=$(realpath -m -- "$OUTPUT")
case "$OUTPUT/" in "$ROOT/"*) echo 'output directory must be outside the repository' >&2; exit 2;; esac
mkdir -p "$OUTPUT/baseline"
[ -z "$(ls -A "$OUTPUT/baseline")" ] || { echo 'baseline output directory must be empty' >&2; exit 2; }
BASELINE=$(git -C "$ROOT" rev-parse --verify "$BASELINE^{commit}")
CANDIDATE=$(git -C "$ROOT" rev-parse HEAD)
git -C "$ROOT" show "$BASELINE:src/zont_analyzer/adapters/ydb/application.py" > "$OUTPUT/baseline/application.py"
git -C "$ROOT" show "$BASELINE:src/zont_analyzer/adapters/ydb/telemetry.py" > "$OUTPUT/baseline/telemetry.py"
git -C "$ROOT" show "$BASELINE:src/zont_analyzer/cloud/monitoring.py" > "$OUTPUT/baseline/monitoring.py"
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
{
    git -C "$ROOT" diff -- src/zont_analyzer/adapters/ydb/application.py src/zont_analyzer/adapters/ydb/telemetry.py src/zont_analyzer/cloud/monitoring.py
} > "$OUTPUT/candidate.patch"
(
    cd "$ROOT"
    sha256sum src/zont_analyzer/adapters/ydb/application.py src/zont_analyzer/adapters/ydb/telemetry.py \
        src/zont_analyzer/adapters/ydb/sample_bounds.py src/zont_analyzer/cloud/monitoring.py \
        src/zont_analyzer/application/ingestion.py tools/measure_ydb_bounds.py deploy/measure-ydb-bounds.sh
) > "$OUTPUT/source-sha256.txt"
docker image inspect "$IMAGE" "$YDB_IMAGE" --format '{{.Id}} {{json .RepoDigests}}' > "$OUTPUT/image-identities.txt"
sh "$ROOT/deploy/assert-ydb-memory.sh" "$PREFIX-ydb" "$IMAGE" > "$OUTPUT/storage-mode.json"
: > "$OUTPUT/results.jsonl"
for YEARS in ${ZONT_BOUNDS_YEARS:-1 5 10}; do
    case "$YEARS" in 1|5|10) ;; *) echo 'years must be 1, 5, 10' >&2; exit 2;; esac
    for LABEL in baseline candidate; do
        BASELINE_OPTION=
        SOURCE_SHA=$CANDIDATE
        if [ "$LABEL" = baseline ]; then BASELINE_OPTION='--baseline-dir /artifacts/baseline'; SOURCE_SHA=$BASELINE; fi
        docker run --rm --name "$PREFIX-run" --network "$PREFIX-net" --cpus 2 --memory 1g --memory-swap 1g \
            --mount "type=bind,src=$ROOT,dst=/workspace,readonly" \
            --mount "type=bind,src=$OUTPUT,dst=/artifacts,readonly" \
            --workdir /tmp --tmpfs /tmp:rw,nosuid,nodev,size=256m \
            -e PYTHONPATH=/workspace/src:/workspace --entrypoint python "$IMAGE" \
            /workspace/tools/measure_ydb_bounds.py --label "$LABEL" --namespace "bounds_${LABEL}_${YEARS}" \
            --source-sha "$SOURCE_SHA" --years "$YEARS" --series "${ZONT_BOUNDS_SERIES:-47}" \
            --repeats "${ZONT_BOUNDS_REPEATS:-3}" $BASELINE_OPTION >> "$OUTPUT/results.jsonl"
    done
done
# Bounded cgroup footprint belongs to this disposable server only.
docker exec "$PREFIX-ydb" sh -c 'cat /sys/fs/cgroup/cpu.stat /sys/fs/cgroup/memory.peak /sys/fs/cgroup/io.stat' > "$OUTPUT/ydb-cgroup-final.txt"
docker run --rm --network none --mount "type=bind,src=$ROOT,dst=/workspace,readonly" \
    --mount "type=bind,src=$OUTPUT,dst=/artifacts,readonly" --entrypoint python "$IMAGE" \
    /workspace/tools/measure_ydb_bounds.py --summarize /artifacts/results.jsonl > "$OUTPUT/summary.json"
echo "Results: $OUTPUT/results.jsonl; summary: $OUTPUT/summary.json"
