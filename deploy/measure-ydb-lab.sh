#!/bin/sh
# Isolated, local YDB benchmark lab with a private output directory.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
COMPOSE_FILE=$ROOT/deploy/ydb-lab/compose.yaml
YDB_PIN=ydbplatform/local-ydb@sha256:9e46fd45875551a75bcf34d0bb9ca0baa1d8763a4ccf2070af45f4467c4b7402
PROM_TAG=prom/prometheus:v3.5.0
GRAFANA_TAG=grafana/grafana:12.1.1
ACTION=${1:-}
OUTPUT=${2:-}

usage() {
    echo "usage: $0 run OUTPUT [BASELINE_SHA] | stop OUTPUT" >&2
    exit 2
}

[ "$ACTION" = run ] || [ "$ACTION" = stop ] || usage
[ -n "$OUTPUT" ] || usage
case "$OUTPUT" in /*) ;; *) echo 'output path must be absolute' >&2; exit 2;; esac
OUTPUT=$(realpath -m -- "$OUTPUT")
case "$OUTPUT/" in "$ROOT/"*) echo 'output must be outside the repository' >&2; exit 2;; esac
PROJECT=$(printf '%s' "$OUTPUT" | cksum | awk '{print "zontydb" $1}')
LOCK=$OUTPUT/images.lock

if [ "$ACTION" = stop ]; then
    [ -r "$LOCK" ] || { echo "missing lab lock file: $LOCK" >&2; exit 2; }
    # This file is created by this script and contains only immutable image refs.
    . "$LOCK"
    export LAB_IMAGE YDB_IMAGE PROMETHEUS_IMAGE GRAFANA_IMAGE
    export LAB_UID=$(id -u) LAB_GID=$(id -g)
    if [ -d "$OUTPUT/candidate-source" ]; then LAB_REPO=$OUTPUT/candidate-source; else LAB_REPO=$ROOT; fi
    export LAB_REPO LAB_OUTPUT=$OUTPUT
    COMPOSE_FILE=$LAB_REPO/deploy/ydb-lab/compose.yaml
    docker compose --project-name "$PROJECT" -f "$COMPOSE_FILE" down --volumes --remove-orphans
    echo "Stopped lab containers and network for $OUTPUT; results were kept."
    exit 0
fi

command -v curl >/dev/null 2>&1 || { echo 'curl is required for local service readiness checks' >&2; exit 2; }
BASELINE=${3:-HEAD}
YEARS=${ZONT_LAB_YEARS:-'1 5 10'}
REPEATS=${ZONT_LAB_REPEATS:-3}
SUITES=${ZONT_LAB_SUITES:-'application gas'}
RUN_TIMEOUT=${ZONT_LAB_RUN_TIMEOUT_SECONDS:-1800}
case "$RUN_TIMEOUT" in ''|*[!0-9]*) echo 'ZONT_LAB_RUN_TIMEOUT_SECONDS must be an integer from 1 to 86400' >&2; exit 2;; esac
[ "$RUN_TIMEOUT" -ge 1 ] && [ "$RUN_TIMEOUT" -le 86400 ] || {
    echo 'ZONT_LAB_RUN_TIMEOUT_SECONDS must be an integer from 1 to 86400' >&2; exit 2;
}
[ -d "$OUTPUT" ] || mkdir -m 700 -p "$OUTPUT"
[ -z "$(ls -A "$OUTPUT")" ] || { echo 'output directory must be empty' >&2; exit 2; }
chmod 700 "$OUTPUT"
umask 077
BASELINE=$(git -C "$ROOT" rev-parse --verify "$BASELINE^{commit}")
CANDIDATE=$(git -C "$ROOT" rev-parse --verify 'HEAD^{commit}')
mkdir "$OUTPUT/baseline"
git -C "$ROOT" archive "$BASELINE" src | tar -x -C "$OUTPUT/baseline"
git -C "$ROOT" diff --binary > "$OUTPUT/candidate.patch"
mkdir "$OUTPUT/candidate-source"
git -C "$ROOT" ls-files -z --cached --others --exclude-standard \
    -- src tools deploy tests pyproject.toml Dockerfile .dockerignore README.md \
    > "$OUTPUT/source-files.nul"
[ -s "$OUTPUT/source-files.nul" ] || { echo 'candidate source selection is empty' >&2; exit 2; }
tar -C "$ROOT" --null --files-from="$OUTPUT/source-files.nul" -cf "$OUTPUT/source.tar"
tar -xf "$OUTPUT/source.tar" -C "$OUTPUT/candidate-source"
rm "$OUTPUT/source.tar"
(cd "$OUTPUT/candidate-source" && find . -type f -print0 | sort -z \
    | xargs -0 -r sha256sum) > "$OUTPUT/source-sha256.txt"
CANDIDATE_SOURCE_SHA=$(sha256sum "$OUTPUT/source-sha256.txt" | awk '{print $1}')
printf '%s\n' "$CANDIDATE_SOURCE_SHA" > "$OUTPUT/candidate-source-sha256.txt"
# Individual public config files are bind-mounted into non-root monitoring
# images. The enclosing output directory remains private (0700).
chmod -R a+rX "$OUTPUT/candidate-source"
chmod -R a-w "$OUTPUT/candidate-source"
COMPOSE_FILE=$OUTPUT/candidate-source/deploy/ydb-lab/compose.yaml
printf '%s\n' "$BASELINE" > "$OUTPUT/baseline-sha.txt"
printf '%s\n' "$CANDIDATE" > "$OUTPUT/candidate-sha.txt"

LAB_TAG="zont-analyzer:ydb-lab-$PROJECT"
docker build --target test --tag "$LAB_TAG" "$OUTPUT/candidate-source"
LAB_IMAGE_ID=$(docker image inspect "$LAB_TAG" --format '{{.Id}}')
docker image save "$LAB_IMAGE_ID" --output "$OUTPUT/lab-image.tar"
gzip "$OUTPUT/lab-image.tar"
LAB_IMAGE=$LAB_IMAGE_ID
docker pull "$YDB_PIN" >/dev/null
docker pull "$PROM_TAG" >/dev/null
docker pull "$GRAFANA_TAG" >/dev/null
YDB_IMAGE=$(docker image inspect "$YDB_PIN" --format '{{index .RepoDigests 0}}')
PROMETHEUS_IMAGE=$(docker image inspect "$PROM_TAG" --format '{{index .RepoDigests 0}}')
GRAFANA_IMAGE=$(docker image inspect "$GRAFANA_TAG" --format '{{index .RepoDigests 0}}')
{
    printf "LAB_IMAGE='%s'\n" "$LAB_IMAGE"
    printf "YDB_IMAGE='%s'\n" "$YDB_IMAGE"
    printf "PROMETHEUS_IMAGE='%s'\n" "$PROMETHEUS_IMAGE"
    printf "GRAFANA_IMAGE='%s'\n" "$GRAFANA_IMAGE"
} > "$LOCK"
export LAB_IMAGE YDB_IMAGE PROMETHEUS_IMAGE GRAFANA_IMAGE
export LAB_UID=$(id -u) LAB_GID=$(id -g)
LAB_REPO=$OUTPUT/candidate-source
export LAB_REPO LAB_OUTPUT=$OUTPUT

case "$REPEATS" in ''|*[!0-9]*) echo 'ZONT_LAB_REPEATS must be an integer from 1 to 10' >&2; exit 2;; esac
[ "$REPEATS" -ge 1 ] && [ "$REPEATS" -le 10 ] || {
    echo 'ZONT_LAB_REPEATS must be an integer from 1 to 10' >&2; exit 2;
}
[ -n "$YEARS" ] || { echo 'ZONT_LAB_YEARS must select at least one year set' >&2; exit 2; }
for year in $YEARS; do
    case "$year" in 1|5|10) ;; *) echo 'ZONT_LAB_YEARS accepts only 1, 5, and 10' >&2; exit 2;; esac
done
[ -n "$SUITES" ] || { echo 'ZONT_LAB_SUITES must select at least one suite' >&2; exit 2; }
for suite in $SUITES; do
    case "$suite" in application|gas) ;; *) echo 'ZONT_LAB_SUITES accepts application and gas' >&2; exit 2;; esac
done
FULL_SCOPE=0
if [ "$YEARS" = '1 5 10' ] && [ "$REPEATS" = 3 ] && [ "$SUITES" = 'application gas' ]; then
    FULL_SCOPE=1
fi
{
    printf 'years=%s\n' "$YEARS"
    printf 'repeats=%s\n' "$REPEATS"
    printf 'suites=%s\n' "$SUITES"
    printf 'full_contract_scope=%s\n' "$FULL_SCOPE"
    printf 'run_timeout_seconds=%s\n' "$RUN_TIMEOUT"
} > "$OUTPUT/run-selection.txt"

assert_lab_image_unchanged() {
    current=$(docker image inspect "$LAB_TAG" --format '{{.Id}}')
    [ "$current" = "$LAB_IMAGE_ID" ] || {
        echo 'pinned local test image tag changed during the lab' >&2
        return 1
    }
}

docker image inspect "$LAB_TAG" "$YDB_IMAGE" "$PROMETHEUS_IMAGE" "$GRAFANA_IMAGE" \
    --format '{{.RepoTags}} {{.Id}} {{.RepoDigests}}' > "$OUTPUT/image-identities.txt"
docker run --rm --network none --entrypoint python "$LAB_IMAGE_ID" -c '
from importlib.metadata import distributions
import json, platform
packages = sorted((d.metadata.get("Name", "unknown"), d.version) for d in distributions())
print(json.dumps({"python": platform.python_version(), "packages": packages}, sort_keys=True))
' > "$OUTPUT/dependencies.json"
{
    printf 'captured_at_utc=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    printf 'git_head=%s\n' "$CANDIDATE"
    printf 'git_dirty='; if [ -n "$(git -C "$ROOT" status --porcelain)" ]; then echo true; else echo false; fi
    printf 'cpu_count=%s\n' "$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc)"
    printf 'kernel=%s\n' "$(uname -srmo)"
    awk '/MemTotal:/ {printf "host_ram_kib=%s\n", $2}' /proc/meminfo
    docker version --format 'docker_client={{.Client.Version}} docker_server={{.Server.Version}}'
    docker info --format 'docker_storage_driver={{.Driver}} docker_cgroup_driver={{.CgroupDriver}} docker_cgroup_version={{.CgroupVersion}}'
} > "$OUTPUT/machine.txt"

STARTED=0
SUCCESS=0
YDB_CONTAINER=
capture_failure_diagnostics() {
    docker compose --project-name "$PROJECT" -f "$COMPOSE_FILE" ps --all \
        > "$OUTPUT/failure-containers.txt" 2>&1 || true
    if [ -n "$YDB_CONTAINER" ]; then
        docker inspect "$YDB_CONTAINER" --format \
            'status={{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} started={{.State.StartedAt}} stopped={{.State.FinishedAt}} image={{.Config.Image}}' \
            > "$OUTPUT/failure-ydb-inspect.txt" 2>&1 || true
        docker logs --tail 100 "$YDB_CONTAINER" \
            > "$OUTPUT/failure-ydb-tail.log" 2>&1 || true
    else
        docker compose --project-name "$PROJECT" -f "$COMPOSE_FILE" logs \
            --no-color --tail 100 ydb > "$OUTPUT/failure-ydb-tail.log" 2>&1 || true
    fi
}
cleanup_failure() {
    status=$?
    trap - EXIT
    if [ "$SUCCESS" -ne 1 ] && [ "$STARTED" -eq 1 ]; then
        capture_failure_diagnostics || true
        docker compose --project-name "$PROJECT" -f "$COMPOSE_FILE" \
            down --volumes --remove-orphans >/dev/null 2>&1 || true
    fi
    exit "$status"
}
trap cleanup_failure EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM

STARTED=1
docker compose --project-name "$PROJECT" -f "$COMPOSE_FILE" up -d ydb exporter prometheus grafana
YDB_CONTAINER=$(docker compose --project-name "$PROJECT" -f "$COMPOSE_FILE" ps -q ydb)

# Verify database readiness with a temporary table in its own namespace.
# This retries real DDL, write and read requests; TCP readiness alone is insufficient.
timeout 90s docker compose --project-name "$PROJECT" -f "$COMPOSE_FILE" run --rm --no-deps \
    -e LAB_READY_SUFFIX="$PROJECT" \
    --entrypoint python runner -c '
import contextlib,json,os,time
from zont_analyzer.adapters.ydb.database import YdbConfig,YdbDatabase
endpoint,database,namespace="grpc://ydb:2136","/local","lab_ready_" + os.environ["LAB_READY_SUFFIX"]
deadline=time.monotonic()+90
attempts=0
last_error=None
while time.monotonic()<deadline:
    attempts+=1
    storage=None
    created=False
    try:
        storage=YdbDatabase(YdbConfig(endpoint,database,namespace,True))
        with contextlib.suppress(Exception): storage.driver.scheme_client.make_directory(storage.path)
        storage.execute("DROP TABLE IF EXISTS readiness_probe;")
        storage.execute("CREATE TABLE readiness_probe (key Utf8 NOT NULL,value Utf8,PRIMARY KEY(key));")
        created=True
        storage.execute("UPSERT INTO readiness_probe (key,value) VALUES (\047probe\047,\047ready\047);")
        rows=storage.execute("SELECT value FROM readiness_probe WHERE key=\047probe\047;")[0].rows
        if len(rows)!=1 or rows[0].value!="ready": raise RuntimeError("probe readback mismatch")
        storage.execute("DROP TABLE readiness_probe;")
        created=False
        storage.driver.scheme_client.remove_directory(storage.path)
        print(json.dumps({"ready":True,"attempts":attempts,"namespace":namespace},sort_keys=True))
        break
    except Exception as exc:
        last_error=type(exc).__name__
        if storage is not None and created:
            with contextlib.suppress(Exception): storage.execute("DROP TABLE IF EXISTS readiness_probe;")
        time.sleep(1)
    finally:
        if storage is not None:
            if created:
                with contextlib.suppress(Exception): storage.execute("DROP TABLE IF EXISTS readiness_probe;")
            with contextlib.suppress(Exception): storage.driver.scheme_client.remove_directory(storage.path)
            with contextlib.suppress(Exception): storage.close()
else:
    raise SystemExit("YDB create/write/read/drop probe timed out after 90s; last error="+str(last_error))
' > "$OUTPUT/ydb-readiness.json"
sh "$ROOT/deploy/assert-ydb-memory.sh" "$YDB_CONTAINER" "$LAB_IMAGE_ID" \
    > "$OUTPUT/storage-mode.json"

PROM_PORT=$(docker compose --project-name "$PROJECT" -f "$COMPOSE_FILE" port prometheus 9090)
GRAFANA_PORT=$(docker compose --project-name "$PROJECT" -f "$COMPOSE_FILE" port grafana 3000)
printf 'prometheus=http://%s\ngrafana=http://%s\n' "$PROM_PORT" "$GRAFANA_PORT" > "$OUTPUT/local-urls.txt"
wait_http() {
    url=$1
    for _ in $(seq 1 60); do
        if curl -fsS "$url" >/dev/null 2>&1; then return 0; fi
        sleep 1
    done
    echo "local lab service did not become ready: $url" >&2
    return 1
}
wait_http "http://$PROM_PORT/-/ready"
wait_http "http://$GRAFANA_PORT/api/health"

assert_lab_image_unchanged
timeout --kill-after=15s "${RUN_TIMEOUT}s" \
docker compose --project-name "$PROJECT" -f "$COMPOSE_FILE" run --rm \
    -e PYTHONPATH=/artifacts/baseline/src:/workspace/src:/workspace runner \
    --output /artifacts --variant baseline --source-sha "$BASELINE" \
    --years $YEARS --repeats "$REPEATS" --suites $SUITES \
    --namespace "${PROJECT}_baseline" \
    > "$OUTPUT/baseline.jsonl"
assert_lab_image_unchanged
timeout --kill-after=15s "${RUN_TIMEOUT}s" \
docker compose --project-name "$PROJECT" -f "$COMPOSE_FILE" run --rm \
    -e PYTHONPATH=/workspace/src:/workspace runner \
    --output /artifacts --variant candidate --source-sha "$CANDIDATE" \
    --years $YEARS --repeats "$REPEATS" --suites $SUITES \
    --namespace "${PROJECT}_candidate" \
    > "$OUTPUT/candidate.jsonl"
docker compose --project-name "$PROJECT" -f "$COMPOSE_FILE" run --rm runner \
    summarize --output /artifacts > "$OUTPUT/summary-command.log"
docker compose --project-name "$PROJECT" -f "$COMPOSE_FILE" run --rm \
    --entrypoint python runner -c '
import json,sys
from pathlib import Path
summary=json.loads(Path("/artifacts/summary.json").read_text())
coverage=summary.get("rpc_coverage") or {}
count_keys=("rpc_count","billed_rpc_count","ru_present_rpc_count","ru_missing_rpc_count",
            "stats_present_rpc_count","stats_missing_rpc_count","cpu_complete_rpc_count",
            "io_complete_rpc_count","failed_rpc_count","control_rpc_count")
if not isinstance(coverage,dict) or any(type(coverage.get(key)) is not int for key in count_keys):
    raise SystemExit("summary lacks compact RPC coverage counts")
if summary.get("hash_parity") is not True:
    raise SystemExit("baseline/candidate fixture or artifact parity failed")
full='"$FULL_SCOPE"'
if full and summary.get("validation_passed") is not True:
    raise SystemExit("full-contract run failed its technical validation gate")
gate={"full_contract_scope": bool(full),
      "hash_parity": summary.get("hash_parity"),
                  "validation_passed": summary.get("validation_passed"),
      "accounting_complete": summary.get("accounting_complete"),
      "accounting_incomplete_reasons": summary.get("accounting_incomplete_reasons", []),
      "rpc_coverage": coverage}
print(json.dumps(gate, sort_keys=True))
if summary.get("accounting_complete") is not True:
    print("WARNING: RU/statistics coverage is incomplete; see summary-gate.json and summary.json",
          file=sys.stderr)
' > "$OUTPUT/summary-gate.json"

SUCCESS=1
echo "Benchmark output: $OUTPUT"
echo "Local Prometheus: http://$PROM_PORT"
echo "Local Grafana: http://$GRAFANA_PORT"
echo "Run '$0 stop $OUTPUT' to remove this lab's containers, network, and Prometheus data."
