#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
suffix=${GITHUB_RUN_ID:-$$}
proxy="zont-auth-nginx-$suffix"
fixture_dir=$(mktemp -d)

cleanup() {
    docker rm -f "$proxy" >/dev/null 2>&1 || true
    rm -rf "$fixture_dir"
}
trap cleanup EXIT INT TERM

install -d "$fixture_dir/www/za/daily"
touch "$fixture_dir/www/za/index.html" "$fixture_dir/www/za/latest.html" \
    "$fixture_dir/www/za/daily/2026-09-05.html"
password_hash=$(openssl passwd -6 'stage16-secret')
printf 'stage16:%s\n' "$password_hash" > "$fixture_dir/zont-analyzer.htpasswd"

docker run -d --rm --name "$proxy" -p 127.0.0.1:18086:18086 \
    -v "$project_root/tests/fixtures/nginx-basic-auth.conf:/etc/nginx/conf.d/default.conf:ro" \
    -v "$project_root/deploy/nginx-zont-analyzer.conf:/etc/nginx/snippets/zont-analyzer.conf:ro" \
    -v "$fixture_dir/zont-analyzer.htpasswd:/etc/nginx/zont-analyzer.htpasswd:ro" \
    -v "$fixture_dir/www:/srv/www:ro" \
    nginx:1.27-alpine >/dev/null

attempt=0
until curl -fsS -u stage16:stage16-secret http://127.0.0.1:18086/za/ >/dev/null; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
        docker logs "$proxy"
        exit 1
    fi
    sleep 1
done

for path in /za/ /za/latest.html /za/daily/ /za/api/health; do
    code=$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:18086$path")
    test "$code" = 401
    curl -fsS -u stage16:stage16-secret "http://127.0.0.1:18086$path" >/dev/null
done

code=$(curl -sS -o /dev/null -w '%{http_code}' -X PUT \
    -H 'Content-Type: application/json' -d '{"status":"applied"}' \
    http://127.0.0.1:18086/za/api/recommendations/example/feedback)
test "$code" = 401
curl -fsS -u stage16:stage16-secret -X PUT \
    -H 'Content-Type: application/json' -d '{"status":"applied"}' \
    http://127.0.0.1:18086/za/api/recommendations/example/feedback >/dev/null
