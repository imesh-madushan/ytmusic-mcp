#!/usr/bin/env bash
# Prepares this checkout to run as a hosted service on a Linux server.
#
#   ./deploy/install.sh --domain mcp.example.com
#
# It works from wherever the repository lives and for whichever user runs it:
#   1. creates a virtualenv and installs the app
#   2. creates .env (with a freshly generated YTMUSIC_SECRET_KEY) if missing
#   3. renders the systemd unit and nginx config into deploy/out/
# It never touches /etc itself; it prints the exact sudo commands to run next.
set -euo pipefail

DOMAIN=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done
[[ -n "$DOMAIN" ]] || { echo "Usage: $0 --domain mcp.example.com" >&2; exit 1; }

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
cd "$APP_DIR"

echo "==> Installing into $APP_DIR/.venv"
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet .

if [[ ! -f .env ]]; then
  echo "==> Creating .env"
  cp .env.example .env
  chmod 600 .env
  KEY="$(.venv/bin/python -m ytmusic_mcp.cli genkey 2>/dev/null)"
  sed -i "s|^YTMUSIC_SECRET_KEY=.*|YTMUSIC_SECRET_KEY=${KEY}|" .env
  sed -i "s|mcp.yourdomain.com|${DOMAIN}|" .env
  echo "    Back up YTMUSIC_SECRET_KEY: without it the stored cookies cannot be decrypted."
fi

env_value() { grep -E "^$1=" .env | tail -n1 | cut -d= -f2- || true; }
PORT="$(env_value MCP_PORT)"; PORT="${PORT:-8787}"
DB_PATH="$(env_value YTMUSIC_DB_PATH)"; DB_PATH="${DB_PATH:-$APP_DIR/data/ytmusic-mcp.db}"
DATA_DIR="$(dirname "$DB_PATH")"
mkdir -p "$DATA_DIR" deploy/out
chmod 700 "$DATA_DIR"

echo "==> Rendering deploy/out/"
sed -e "s|@USER@|${RUN_USER}|g" -e "s|@APP_DIR@|${APP_DIR}|g" -e "s|@DATA_DIR@|${DATA_DIR}|g" \
  deploy/ytmusic-mcp.service.template > deploy/out/ytmusic-mcp.service
sed -e "s|\${MCP_DOMAIN}|${DOMAIN}|g" -e "s|\${MCP_PORT}|${PORT}|g" \
  deploy/nginx.conf.template > deploy/out/ytmusic-mcp.nginx.conf

cat <<EOF

Done. Next steps:

  # 1. Review .env (public URL, port), then create your first user:
  $APP_DIR/.venv/bin/python -m ytmusic_mcp.cli add-user <name>
  $APP_DIR/.venv/bin/python -m ytmusic_mcp.cli set-cookies <name>

  # 2. Start the service
  sudo cp deploy/out/ytmusic-mcp.service /etc/systemd/system/
  sudo systemctl daemon-reload && sudo systemctl enable --now ytmusic-mcp

  # 3. Put nginx in front (HTTPS)
  sudo cp deploy/out/ytmusic-mcp.nginx.conf /etc/nginx/sites-available/ytmusic-mcp
  sudo ln -s /etc/nginx/sites-available/ytmusic-mcp /etc/nginx/sites-enabled/
  sudo certbot --nginx -d ${DOMAIN}
  sudo nginx -t && sudo systemctl reload nginx

  # 4. Optional: ban repeated failed logins
  sudo cp deploy/fail2ban-filter.conf /etc/fail2ban/filter.d/ytmusic-mcp.conf
  sudo cp deploy/fail2ban-jail.conf /etc/fail2ban/jail.d/ytmusic-mcp.conf
  sudo systemctl restart fail2ban
EOF
