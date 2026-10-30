#!/usr/bin/env bash
# Guided install of the YouTube Music MCP server on a Linux machine.
#
#   ./deploy/install.sh                       # first install: asks a few questions
#   ./deploy/install.sh --domain mcp.example.com --dry-run
#
# Later, on a server that is already set up:
#   ./deploy/install.sh update                # quick: pull, update packages, restart
#   ./deploy/install.sh rebuild               # full: also builds the UI apps, re-applies config
#   ./deploy/install.sh restart | status
#
# For a first install this script prepares Python (a virtualenv and the packages)
# and hands over to the setup wizard: settings, your first user, the web server
# (Caddy or nginx), and the systemd service. Every option is passed through; see
# `python -m ytmusic_mcp.cli setup --help` (or `... update --help`).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

# Maintenance commands use the environment that already exists.
case "${1:-}" in
  update|rebuild|restart|status)
    if [[ ! -x .venv/bin/python ]]; then
      echo "No environment found here. Run ./deploy/install.sh first (without a command)." >&2
      exit 1
    fi
    exec .venv/bin/python -m ytmusic_mcp.cli "$@"
    ;;
esac

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
