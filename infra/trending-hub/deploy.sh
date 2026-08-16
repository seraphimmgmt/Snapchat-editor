#!/usr/bin/env bash
# Deploy / update the Trending Hub on the droplet.
#
#   ./infra/trending-hub/deploy.sh            # first install or update
#
# Idempotent: creates /opt/trending-hub + venv + systemd unit on first run,
# just copies files + restarts on later runs. Never touches render-server.
# The admin token is generated once and printed — save it in the app's
# ⚙ → HUB section. Re-running never rotates it.
set -euo pipefail
HOST="${HUB_HOST:-root@137.184.47.65}"
PORT="${HUB_PORT:-8790}"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "→ copying files to $HOST:/opt/trending-hub"
ssh "$HOST" 'mkdir -p /opt/trending-hub'
scp -q "$HERE/main.py" "$HERE/report.html" "$HOST:/opt/trending-hub/"

ssh "$HOST" bash -s "$PORT" <<'REMOTE'
set -euo pipefail
PORT="$1"
cd /opt/trending-hub
if [ ! -x venv/bin/uvicorn ]; then
  echo "→ creating venv + installing fastapi/uvicorn"
  python3 -m venv venv
  ./venv/bin/pip install -q --upgrade pip
  ./venv/bin/pip install -q fastapi uvicorn
fi
if [ ! -s admin_token ]; then
  echo "→ generating admin token"
  python3 -c 'import secrets; print("sa_" + secrets.token_urlsafe(30))' > admin_token
  chmod 600 admin_token
fi
if [ ! -f /etc/systemd/system/trending-hub.service ]; then
  echo "→ installing systemd unit"
  cat > /etc/systemd/system/trending-hub.service <<UNIT
[Unit]
Description=Seraphim Studio Trending Hub
After=network.target

[Service]
WorkingDirectory=/opt/trending-hub
Environment=TRENDING_HUB_DIR=/opt/trending-hub
ExecStart=/opt/trending-hub/venv/bin/uvicorn main:app --host 0.0.0.0 --port ${PORT}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable trending-hub >/dev/null
fi
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw status | grep -q "${PORT}/tcp" || { echo "→ opening ufw ${PORT}/tcp"; ufw allow "${PORT}/tcp" >/dev/null; }
fi
systemctl restart trending-hub
sleep 2
systemctl is-active --quiet trending-hub && echo "→ trending-hub active" || { echo "!! trending-hub failed:"; journalctl -u trending-hub -n 30 --no-pager; exit 1; }
echo "→ admin token (save this in the app ⚙ → HUB):"
cat admin_token
REMOTE

echo "→ external health check"
curl -sf "http://${HOST#*@}:${PORT}/health" && echo
echo "→ report URL: http://${HOST#*@}:${PORT}/trending/report.html"
