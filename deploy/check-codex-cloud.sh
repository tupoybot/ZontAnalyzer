#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VENV=${ZONT_CODEX_VENV:-"$HOME/.cache/zont-analyzer-codex/venv"}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

cd "$ROOT"
export PYTHONDONTWRITEBYTECODE=1
export RUFF_CACHE_DIR="$TMP/ruff"
export MYPY_CACHE_DIR="$TMP/mypy"

"$VENV/bin/ruff" check .
"$VENV/bin/mypy" src/zont_analyzer
"$VENV/bin/pytest" -ra -p no:cacheprovider
"$VENV/bin/python" -m build --no-isolation --outdir "$TMP/dist"
