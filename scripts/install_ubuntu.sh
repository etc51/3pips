#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/opt/3pips}"
SERVICE_NAME="${SERVICE_NAME:-3pips-paper}"
BOT_USER="${BOT_USER:-3pips}"
PYTHON_BIN="${PYTHON_BIN:-}"

if [[ "$(pwd)" != "$PROJECT_ROOT" ]]; then
  echo "Run this script from $PROJECT_ROOT"
  echo "Current directory: $(pwd)"
  exit 1
fi

if ! id "${BOT_USER}" >/dev/null 2>&1; then
  sudo useradd --system --create-home --shell /bin/bash "${BOT_USER}"
fi

if [[ -z "${PYTHON_BIN}" ]]; then
  for candidate in python3.12 python3.11 python3; do
    if command -v "${candidate}" >/dev/null 2>&1; then
      PYTHON_BIN="$(command -v "${candidate}")"
      break
    fi
  done
fi

if [[ -z "${PYTHON_BIN}" ]]; then
  echo "Python not found. Install Python 3.11+ first."
  exit 1
fi

"${PYTHON_BIN}" - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit(
        f"Python 3.11+ required. Current: {sys.version.split()[0]}. "
        "Install python3.11/python3.12 and rerun."
    )
PY

sudo chown -R "${BOT_USER}:${BOT_USER}" "${PROJECT_ROOT}"
sudo -u "${BOT_USER}" mkdir -p reports/runtime reports/paper_runs/v7_live_20260525 reports/archives

sudo -u "${BOT_USER}" "${PYTHON_BIN}" -m venv "${PROJECT_ROOT}/.venv"
sudo -u "${BOT_USER}" "${PROJECT_ROOT}/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
sudo -u "${BOT_USER}" "${PROJECT_ROOT}/.venv/bin/python" -m pip install -r requirements.txt
sudo -u "${BOT_USER}" "${PROJECT_ROOT}/.venv/bin/python" -m pip install \
  --extra-index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple \
  t-tech-investments
sudo -u "${BOT_USER}" "${PROJECT_ROOT}/.venv/bin/python" - <<'PY'
from t_tech.invest import Client
print("t_tech.invest import ok")
PY

for name in classic_core gl_watch neo tail_research; do
  path="reports/paper_runs/v7_live_20260525/${name}_paper_open_positions.json"
  if [[ ! -f "$path" ]]; then
    sudo -u "${BOT_USER}" bash -c "printf '[]\n' > '$path'"
  fi
done

sudo mkdir -p /etc/3pips

sudo cp "deploy/${SERVICE_NAME}.service" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo cp "deploy/3pips-archive.service" "/etc/systemd/system/3pips-archive.service"
sudo cp "deploy/3pips-archive.timer" "/etc/systemd/system/3pips-archive.timer"
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl enable 3pips-archive.timer

echo "Installed ${SERVICE_NAME}."
echo "Start:  sudo systemctl start ${SERVICE_NAME}"
echo "Logs:   journalctl -u ${SERVICE_NAME} -f"
echo "Daily archives: /opt/3pips/reports/archives/"
