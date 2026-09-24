#!/bin/sh
set -eu

# Run in the Codex Cloud task container, including when its cache is resumed.
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VENV=${ZONT_CODEX_VENV:-"$HOME/.cache/zont-analyzer-codex/venv"}
GITHUB_REPO_URL=https://github.com/tupoybot/ZontAnalyzer.git

python3 -c 'import sys; assert sys.version_info[:2] == (3, 12), "Select Python 3.12 in Codex Cloud settings"'
python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install -e "$ROOT[dev]" 'hatchling>=1.25'

# Codex Cloud checkouts may not have a usable origin even when the GitHub connector
# can read the repository and post PR comments. Keep a normal Git remote available
# so automated issue tasks can publish their commits to the pre-created PR branch.
if git -C "$ROOT" remote get-url origin >/dev/null 2>&1; then
    git -C "$ROOT" remote set-url origin "$GITHUB_REPO_URL"
else
    git -C "$ROOT" remote add origin "$GITHUB_REPO_URL"
fi

# Add a fine-grained GitHub PAT as the Codex Environment secret GITHUB_TOKEN.
# Codex exposes secrets only during setup. Persist authentication through gh's Git
# credential helper, then remove the shell variables before the agent phase.
if [ -n "${GITHUB_TOKEN:-}" ]; then
    command -v gh >/dev/null 2>&1 || {
        printf '%s\n' 'GITHUB_TOKEN is configured but GitHub CLI (gh) is unavailable.' >&2
        exit 1
    }

    github_token=$GITHUB_TOKEN
    unset GITHUB_TOKEN
    printf '%s\n' "$github_token" | gh auth login --hostname github.com --with-token
    gh auth setup-git
    github_token=
else
    printf '%s\n'         'WARNING: Codex Cloud secret GITHUB_TOKEN is not configured; automated tasks can edit locally but cannot reliably push to GitHub.' >&2
fi

git -C "$ROOT" ls-remote origin HEAD >/dev/null
printf 'Codex Cloud tools ready: %s\n' "$VENV"
printf 'Git remote ready: %s\n' "$(git -C "$ROOT" remote get-url origin)"
