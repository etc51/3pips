#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/opt/3pips}"
SERVICE_NAME="${SERVICE_NAME:-3pips-paper}"
BOT_USER="${BOT_USER:-3pips}"

if [[ "$(pwd)" != "$PROJECT_ROOT" ]]; then
  echo "Run this script from $PROJECT_ROOT"
  echo "Current directory: $(pwd)"
  exit 1
fi

if ! id "${BOT_USER}" >/dev/null 2>&1; then
  sudo useradd --system --create-home --shell /bin/bash "${BOT_USER}"
fi

sudo chown -R "${BOT_USER}:${BOT_USER}" "${PROJECT_ROOT}"
sudo -u "${BOT_USER}" mkdir -p reports/runtime reports/paper_runs/v7_live_20260525

sudo -u "${BOT_USER}" python3 -m venv "${PROJECT_ROOT}/.venv"
sudo -u "${BOT_USER}" "${PROJECT_ROOT}/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
sudo -u "${BOT_USER}" "${PROJECT_ROOT}/.venv/bin/python" -m pip install -r requirements.txt

for name in classic_core gl_watch neo tail_research stock_watch; do
  path="reports/paper_runs/v7_live_20260525/${name}_paper_open_positions.json"
  if [[ ! -f "$path" ]]; then
    sudo -u "${BOT_USER}" bash -c "printf '[]\n' > '$path'"
  fi
done

sudo mkdir -p /etc/3pips
if [[ ! -f /etc/3pips/3pips.env ]]; then
  sudo cp deploy/3pips.env.example /etc/3pips/3pips.env
  sudo chmod 600 /etc/3pips/3pips.env
  echo "Created /etc/3pips/3pips.env. Put the token there if it is not already configured."
fi

sudo cp "deploy/${SERVICE_NAME}.service" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"

echo "Installed ${SERVICE_NAME}."
echo "Start:  sudo systemctl start ${SERVICE_NAME}"
echo "Logs:   journalctl -u ${SERVICE_NAME} -f"
