#!/usr/bin/env bash
# opengcp installer (Linux / macOS).
#
# opengcp is source-available (not published to PyPI). Install it from the
# git repository. This script tries pipx, then uv, then plain pip, and finally
# falls back to a local editable install if you have already cloned the repo.
set -euo pipefail

REPO="git+https://github.com/cognis-digital/opengcp.git"

echo "opengcp installer"
echo "-----------------"

if command -v pipx >/dev/null 2>&1; then
    echo "==> installing with pipx"
    pipx install "$REPO"
elif command -v uv >/dev/null 2>&1; then
    echo "==> installing with uv"
    uv tool install "$REPO"
elif command -v pip3 >/dev/null 2>&1 || command -v pip >/dev/null 2>&1; then
    PIP="$(command -v pip3 || command -v pip)"
    echo "==> installing with pip ($PIP)"
    "$PIP" install --user "$REPO"
else
    echo "No pipx/uv/pip found. If you have cloned the repo, run:"
    echo "    python -m pip install ."
    exit 1
fi

echo
echo "Done. Try:  opengcp version"
echo "Then:       opengcp serve --port 8085"
