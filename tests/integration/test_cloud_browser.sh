#!/bin/sh
# Browser acceptance against isolated YDB and a private in-memory S3 transport.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
image=${ZONT_TEST_IMAGE:-zont-analyzer:test-local}
port=${ZONT_CLOUD_BROWSER_PORT:-18087}
prefix="zont-cloud-browser-$$"
network="$prefix-net"
ydb="$prefix-ydb"
app="$prefix-app"

cleanup() {
    docker rm -f "$app" "$ydb" >/dev/null 2>&1 || true
    docker network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

test -d "$root/tests/integration/browser/node_modules/playwright" || {
    echo "Install pinned browser dependencies: npm ci --prefix tests/integration/browser" >&2
    exit 2
}
docker network create "$network" >/dev/null
docker run -d --name "$ydb" --hostname "$ydb" --network "$network" --memory 5g --memory-swap 5g --cpus 2 \
    -e GRPC_PORT=2136 -e MON_PORT=8765 -e YDB_USE_IN_MEMORY_PDISKS=true \
    -e YDB_DEFAULT_LOG_LEVEL=WARN \
    ydbplatform/local-ydb@sha256:9e46fd45875551a75bcf34d0bb9ca0baa1d8763a4ccf2070af45f4467c4b7402 >/dev/null

docker run --rm --network "$network" --entrypoint python "$image" -c '
import socket,sys,time
deadline=time.monotonic()+90
while time.monotonic()<deadline:
    try:
        with socket.create_connection((sys.argv[1],2136),timeout=1): break
    except OSError: time.sleep(1)
else: raise SystemExit("YDB did not become ready")
' "$ydb"
sh "$root/deploy/assert-ydb-memory.sh" "$ydb" "$image"

docker run -d --name "$app" --network "$network" -p "127.0.0.1:$port:8080" \
    --cpus 2 --memory 1g --memory-swap 1g --workdir /tmp \
    --mount "type=bind,src=$root,dst=/workspace,readonly" \
    --tmpfs /tmp:rw,exec,nosuid,nodev,size=1g \
    -e PYTHONPATH=/workspace/src:/workspace \
    -e "YDB_ENDPOINT=grpc://$ydb:2136" -e YDB_DATABASE=/local \
    -e YDB_NAMESPACE=cloud_browser -e YDB_ANONYMOUS_CREDENTIALS=1 \
    -e "CLOUD_PUBLIC_ORIGIN=http://127.0.0.1:$port" \
    --entrypoint python "$image" /workspace/tests/integration/cloud_browser_server.py >/dev/null

attempt=0
until curl -fsS -u browser:fixture-secret "http://127.0.0.1:$port/ready" >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 60 ]; then
        docker logs "$app"
        exit 1
    fi
    sleep 1
done
ZONT_CLOUD_BROWSER_URL="http://127.0.0.1:$port" node "$root/tests/integration/cloud_browser.mjs"
