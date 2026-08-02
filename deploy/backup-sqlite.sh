#!/bin/sh
set -eu

release_dir=${1:-/opt/zont-analyzer/current}

# Use SQLite's online backup API through the running application. Copying a live
# WAL database with cp/tar can produce an inconsistent backup.
docker compose --project-name zont-analyzer \
    -f "$release_dir/deploy/compose.yaml" \
    -f "$release_dir/deploy/compose.test.yaml" \
    exec -T worker zont-analyzer --config /config/config.yaml --data-dir /data db backup

docker compose --project-name zont-analyzer \
    -f "$release_dir/deploy/compose.yaml" \
    -f "$release_dir/deploy/compose.test.yaml" \
    exec -T worker zont-analyzer --config /config/config.yaml --data-dir /data doctor
