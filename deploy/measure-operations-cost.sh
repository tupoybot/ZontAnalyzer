#!/bin/sh
# Disposable synthetic workload; baseline application source is never monkeypatched.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BASELINE=${1:?usage: deploy/measure-operations-cost.sh BASELINE_SHA OUTPUT_DIRECTORY}
OUTPUT=${2:?output directory is required}
IMAGE=${ZONT_TEST_IMAGE:-zont-analyzer:test-local}
YDB_IMAGE=ydbplatform/local-ydb@sha256:9e46fd45875551a75bcf34d0bb9ca0baa1d8763a4ccf2070af45f4467c4b7402
PREFIX="zont-operations-$$"
case "$OUTPUT" in /*) ;; *) echo 'output directory must be absolute' >&2; exit 2;; esac
OUTPUT=$(realpath -m -- "$OUTPUT")
case "$OUTPUT/" in "$ROOT/"*) echo 'output directory must be outside the repository' >&2; exit 2;; esac
mkdir -p "$OUTPUT"
[ -z "$(ls -A "$OUTPUT")" ] || { echo 'output directory must be empty' >&2; exit 2; }
BASELINE=$(git -C "$ROOT" rev-parse --verify "$BASELINE^{commit}")
CANDIDATE=$(git -C "$ROOT" rev-parse HEAD)
mkdir "$OUTPUT/baseline"
git -C "$ROOT" archive "$BASELINE" src | tar -x -C "$OUTPUT/baseline"
git -C "$ROOT" diff -- src tools/benchmark_operations_cost.py deploy/measure-operations-cost.sh > "$OUTPUT/candidate.patch"
(cd "$ROOT" && find src -type f -name '*.py' -exec sha256sum {} +) > "$OUTPUT/source-sha256.txt"
(cd "$ROOT" && sha256sum tools/benchmark_operations_cost.py tools/benchmark_ydb_cost.py \
    tools/measure_ydb_bounds.py deploy/measure-operations-cost.sh) >> "$OUTPUT/source-sha256.txt"
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
docker image inspect "$IMAGE" "$YDB_IMAGE" --format '{{.Id}} {{json .RepoDigests}}' > "$OUTPUT/image-identities.txt"
sh "$ROOT/deploy/assert-ydb-memory.sh" "$PREFIX-ydb" "$IMAGE" > "$OUTPUT/storage-mode.json"
for LABEL in baseline candidate; do
    SOURCE=/workspace/src
    SHA=$CANDIDATE
    if [ "$LABEL" = baseline ]; then SOURCE=/artifacts/baseline/src; SHA=$BASELINE; fi
    docker run --rm --name "$PREFIX-run" --network "$PREFIX-net" --user "$(id -u):$(id -g)" \
        --cpus 2 --memory 1g --memory-swap 1g \
        --mount "type=bind,src=$ROOT,dst=/workspace,readonly" \
        --mount "type=bind,src=$OUTPUT,dst=/artifacts" --workdir /tmp \
        --tmpfs /tmp:rw,nosuid,nodev,size=256m -e "PYTHONPATH=$SOURCE:/workspace" \
        --entrypoint python "$IMAGE" /workspace/tools/benchmark_operations_cost.py \
        --label "$LABEL" --source-sha "$SHA" --namespace "operations_$LABEL" \
        --days "${ZONT_OPERATIONS_DAYS:-3}" --output "/artifacts/$LABEL-evidence.json" > "$OUTPUT/$LABEL.jsonl"
done
docker run --rm --network none --mount "type=bind,src=$OUTPUT,dst=/artifacts,readonly" \
    --entrypoint python "$IMAGE" -c '
import json
from pathlib import Path
base=json.loads(Path("/artifacts/baseline-evidence.json").read_text())
candidate=json.loads(Path("/artifacts/candidate-evidence.json").read_text())
def differences(a,b,path=""):
    if type(a) is not type(b): return [path]
    if isinstance(a,dict):
        result=[]
        for key in sorted(a.keys()|b.keys()):
            result.extend([path+"/"+key] if key not in a or key not in b else differences(a[key],b[key],path+"/"+key))
        return result
    if isinstance(a,list):
        if len(a)!=len(b): return [path]
        return [p for i,(x,y) in enumerate(zip(a,b)) for p in differences(x,y,path+"/"+str(i))]
    return [] if a==b else [path]
costs={}
for label in ("baseline","candidate"):
    records=[json.loads(line) for line in Path("/artifacts/"+label+".jsonl").read_text().splitlines()]
    costs[label]={r["operation"]: {"elapsed_seconds":r["elapsed_seconds"],"native":r["native"]}
                  for r in records if "native" in r}
print(json.dumps({"full_reports_equal":base["reports"]==candidate["reports"],
                  "full_artifacts_equal":base["artifacts"]==candidate["artifacts"],
                  "difference_paths":differences(base,candidate),"operations":costs},sort_keys=True))
assert base==candidate, "full canonical reports or normalized artifacts differ; inspect evidence"
' > "$OUTPUT/summary.json"
echo "Results: $OUTPUT; local server RU are not managed billing."
