#!/bin/sh
set -eu

# Run in the Codex Cloud task container, including when its cache is resumed.
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VENV=${ZONT_CODEX_VENV:-"$HOME/.cache/zont-analyzer-codex/venv"}

python3 -c 'import sys; assert sys.version_info[:2] == (3, 12), "Select Python 3.12 in Codex Cloud settings"'
python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install -e "$ROOT[dev]" 'hatchling>=1.25'

printf 'Codex Cloud tools ready: %s\n' "$VENV"
