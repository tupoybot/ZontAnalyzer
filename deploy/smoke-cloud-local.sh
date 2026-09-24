#!/bin/sh
# No external network, real credentials, databases, or production data.
set -eu
image=${1:?Usage: smoke-cloud-local.sh IMAGE}
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
name="zont-cloud-smoke-$$"
cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM
xray='{"inbounds":[{"listen":"127.0.0.1","port":1080,"protocol":"http"}],"outbounds":[{"protocol":"vless","settings":{"vnext":[{"address":"127.0.0.1","port":9,"users":[{"id":"00000000-0000-0000-0000-000000000001","encryption":"none"}]}]}}]}'
docker run -d --name "$name" --network none --read-only --memory 512m --cpus 1 --pids-limit 64 \
    --security-opt no-new-privileges --cap-drop ALL --tmpfs /tmp:rw,nosuid,nodev,size=32m \
    -e CLOUD_ENVIRONMENT=dev -e CLOUD_WEB_CREDENTIALS=smoke:synthetic \
    -e CLOUD_JOB_TIMEOUT_SECONDS=15 -e "XRAY_CONFIG=$xray" "$image" >/dev/null
attempt=0
until docker exec "$name" curl -fsS -u smoke:synthetic http://127.0.0.1:8080/ready >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -gt 40 ]; then
        docker logs "$name"
        exit 1
    fi
    sleep 0.25
done
docker exec "$name" sh -c 'test ! -e /data && test ! -e /publish; rm -f /tmp/zont-xray-*.json'
docker exec -i "$name" python - <"$root/tests/integration/cloud_runtime_smoke.py"
docker exec -i "$name" python - <"$root/tests/integration/cloud_metadata_credentials_smoke.py"
docker exec "$name" python -c 'import sys; from zont_analyzer.cloud import analytics, runtime; assert "sqlite3" not in sys.modules; assert "sqlalchemy" not in sys.modules'
