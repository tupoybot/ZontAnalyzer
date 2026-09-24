#!/bin/sh
# Inspect the generated server configuration, not just its requested environment.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
[ "$#" -eq 2 ] || { echo "Usage: $0 YDB_CONTAINER TEST_IMAGE" >&2; exit 2; }
config=$(docker exec "$1" cat /ydb_data/cluster/kikimr_configs/config.yaml)
printf '%s\n' "$config" | docker run --rm -i --network none \
    --mount "type=bind,src=$ROOT/tools/assert_ydb_memory.py,dst=/verify.py,readonly" \
    --entrypoint python "$2" /verify.py
