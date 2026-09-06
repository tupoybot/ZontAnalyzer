#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
suffix=${GITHUB_RUN_ID:-$$}
proxy="zont-archive-nginx-$suffix"
backend="zont-archive-backend-$suffix"
fixture_dir=$(mktemp -d)

cleanup() {
    docker rm -f "$backend" "$proxy" >/dev/null 2>&1 || true
    # Files are created by the image's unprivileged UID, so remove them from a
    # short-lived root container before removing the host temporary directory.
    docker run --rm -v "$fixture_dir:/fixture" nginx:1.27-alpine sh -c 'rm -rf /fixture/*' >/dev/null 2>&1 || true
    rm -rf "$fixture_dir"
}
trap cleanup EXIT INT TERM

test -d "$project_root/tests/integration/browser/node_modules/playwright" || {
    echo "Install pinned browser test dependencies first: npm install --prefix tests/integration/browser" >&2
    exit 2
}

install -d "$fixture_dir/data" "$fixture_dir/publish"
# The production image runs as the unprivileged zont UID; these are disposable
# bind mounts shared with nginx and removed by cleanup.
chmod 0777 "$fixture_dir/data" "$fixture_dir/publish"
cat > "$fixture_dir/config.yaml" <<'EOF'
pilot:
  reports_dir: /publish
feedback:
  listen_host: 127.0.0.1
  listen_port: 8787
  public_api_base_url: /za/api
EOF
password_hash=$(openssl passwd -6 'stage18-secret')
printf 'stage18:%s\n' "$password_hash" > "$fixture_dir/zont-analyzer.htpasswd"
cat > "$fixture_dir/default.conf" <<'EOF'
server {
    listen 18086;
    server_name localhost;
    include /etc/nginx/snippets/zont-analyzer-root.conf;

    location ^~ /za/api/ {
        auth_basic "ZontAnalyzer";
        auth_basic_user_file /etc/nginx/zont-analyzer.htpasswd;
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    location = /za/daily/ {
        root /var/www/html;
        autoindex on;
    }
    location ^~ /za/ {
        root /var/www/html;
        try_files $uri $uri/ =404;
    }
}
EOF

docker run -d --rm --name "$proxy" -p 127.0.0.1:18086:18086 \
    -v "$fixture_dir/default.conf:/etc/nginx/conf.d/default.conf:ro" \
    -v "$project_root/deploy/nginx-zont-analyzer-root.conf:/etc/nginx/snippets/zont-analyzer-root.conf:ro" \
    -v "$fixture_dir/zont-analyzer.htpasswd:/etc/nginx/zont-analyzer.htpasswd:ro" \
    -v "$fixture_dir/publish:/var/www/html/za:ro" nginx:1.27-alpine >/dev/null

docker run --rm --entrypoint python -v "$fixture_dir/data:/data" -v "$fixture_dir/publish:/publish" \
    -v "$fixture_dir/config.yaml:/config/config.yaml:ro" \
    -v "$project_root/tests/integration/archive_browser_fixture.py:/fixture.py:ro" \
    zont-analyzer:stage18-candidate /fixture.py

docker run -d --rm --name "$backend" --network "container:$proxy" \
    -v "$fixture_dir/data:/data" -v "$fixture_dir/publish:/publish" \
    -v "$fixture_dir/config.yaml:/config/config.yaml:ro" \
    -v "$project_root/tests/integration/archive_browser_feedback_server.py:/feedback-server.py:ro" \
    --entrypoint python zont-analyzer:stage18-candidate /feedback-server.py >/dev/null

attempt=0
until curl -fsS -u stage18:stage18-secret http://127.0.0.1:18086/api/health >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
        docker logs "$proxy"
        docker logs "$backend"
        exit 1
    fi
    sleep 1
done

node "$project_root/tests/integration/archive_browser.mjs"
