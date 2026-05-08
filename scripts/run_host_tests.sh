#!/bin/bash
# Run host-side tests on the Pi 4 build host.
# Does NOT touch hardware. Does NOT need root.
#
# Works regardless of where this script lives in the project tree —
# it walks up looking for pyproject.toml. Place it at the project
# root or in scripts/, doesn't matter.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Locate the project root by walking up to the first pyproject.toml.
PROJECT_ROOT="$SCRIPT_DIR"
while [ ! -f "$PROJECT_ROOT/pyproject.toml" ]; do
    parent="$(dirname "$PROJECT_ROOT")"
    if [ "$parent" = "$PROJECT_ROOT" ]; then
        echo "Error: could not find pyproject.toml above $SCRIPT_DIR" >&2
        exit 1
    fi
    PROJECT_ROOT="$parent"
done

cd "$PROJECT_ROOT"
echo "[setup] project root: $PROJECT_ROOT"

if [ ! -d .venv ]; then
    echo "[setup] creating .venv ..."
    python3 -m venv .venv
fi

# shellcheck disable=SC1091
. .venv/bin/activate

# Always re-run pip install -e .[dev]. It's idempotent and fast when
# nothing has changed, but ensures any new deps added to pyproject.toml
# get pulled in even if a stale venv from a previous step exists.
echo "[setup] syncing dev deps ..."
pip install --quiet --upgrade pip
pip install --quiet -e '.[dev]'

exec pytest tests/ -v "$@"
