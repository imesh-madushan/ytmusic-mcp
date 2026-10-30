#!/usr/bin/env bash
# Guided install of the YouTube Music MCP server on a Linux machine.
#
#   ./deploy/install.sh                       # asks a few questions
#   ./deploy/install.sh --domain mcp.example.com --dry-run
#
# This script only prepares Python (a virtualenv and the packages) and then hands
# over to the setup wizard, which does the rest: settings, your first user, the
# web server (Caddy or nginx), and the systemd service. Every option is passed
# through; see `python -m ytmusic_mcp.cli setup --help`.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

if ! command -v python3 >/dev/null; then
  echo "python3 is required (3.10 or newer). On Ubuntu: sudo apt install python3 python3-venv" >&2
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  echo "Preparing Python environment in $APP_DIR/.venv ..."
  python3 -m venv .venv || {
    echo "Could not create a virtualenv. On Ubuntu: sudo apt install python3-venv" >&2
    exit 1
  }
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e .

exec .venv/bin/python -m ytmusic_mcp.cli setup "$@"
