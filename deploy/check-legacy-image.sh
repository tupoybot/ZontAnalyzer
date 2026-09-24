#!/bin/sh
# Validate the already pulled artifact before touching legacy state or configuration.
set -eu
image=${1:?Usage: check-legacy-image.sh IMAGE}
revision=$(docker image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')
runtime=$(docker image inspect "$image" --format '{{index .Config.Labels "org.zont.runtime"}}')
case "$runtime" in
    ''|legacy) ;;
    *) echo 'Native YDB images cannot run on legacy production' >&2; exit 1 ;;
esac
case "$revision" in ''|*[!0-9a-f]*) echo 'Image revision is missing or invalid' >&2; exit 1 ;; esac
[ "${#revision}" = 40 ] || exit 1
repository=${image#ghcr.io/}
repository=${repository%@*}
# Public repository: no credentials are needed. Fail closed if provenance cannot be checked.
comparison=$(curl --fail --silent --show-error --max-time 15 \
  "https://api.github.com/repos/$repository/compare/$revision...old-stable")
printf '%s' "$comparison" | python3 -c '
import json, sys
comparison = json.load(sys.stdin)
if comparison.get("status") not in ("ahead", "identical"):
    raise SystemExit("Image commit does not belong to old-stable")
'
printf 'Legacy image belongs to old-stable.\n'
