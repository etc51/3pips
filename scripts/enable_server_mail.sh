#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/opt/3pips}"
ENV_FILE="${ENV_FILE:-/etc/3pips/3pips.env}"
SECRETS_DIR="${SECRETS_DIR:-/opt/3pips/secrets}"
PASSWORD_FILE="${PASSWORD_FILE:-/opt/3pips/secrets/archive_smtp_password.txt}"
EMAIL="etc00051@yandex.ru"
DAILY_TIME="23:59"
TEST_NOW=0
PASSWORD=""
READ_STDIN=0
PASSWORD_B64=""

usage() {
  cat <<'EOF'
Usage:
  bash scripts/enable_server_mail.sh [options]

Options:
  --email EMAIL             Recipient/sender mailbox. Default: etc00051@yandex.ru
  --daily-time HH:MM        Daily archive send time. Default: 23:59
  --password VALUE          SMTP password directly
  --password-b64 VALUE      SMTP password in base64
  --password-stdin          Read SMTP password from stdin
  --test-now                Immediately build and send today's raw archive
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --email)
      EMAIL="$2"
      shift 2
      ;;
    --daily-time)
      DAILY_TIME="$2"
      shift 2
      ;;
    --password)
      PASSWORD="$2"
      shift 2
      ;;
    --password-b64)
      PASSWORD_B64="$2"
      shift 2
      ;;
    --password-stdin)
      READ_STDIN=1
      shift
      ;;
    --test-now)
      TEST_NOW=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$PASSWORD" && -n "$PASSWORD_B64" ]]; then
  PASSWORD="$(printf '%s' "$PASSWORD_B64" | python3 - <<'PY'
import base64
import sys
data = sys.stdin.read().strip()
print(base64.b64decode(data).decode("utf-8"), end="")
PY
)"
fi

if [[ -z "$PASSWORD" && "$READ_STDIN" -eq 1 ]]; then
  IFS= read -r PASSWORD || true
fi

if [[ -z "$PASSWORD" ]]; then
  read -r -s -p "SMTP password: " PASSWORD
  echo
fi

if [[ -z "$PASSWORD" ]]; then
  echo "SMTP password is empty" >&2
  exit 1
fi

sudo mkdir -p /etc/3pips "$SECRETS_DIR"
sudo touch "$ENV_FILE"

printf '%s' "$PASSWORD" | sudo tee "$PASSWORD_FILE" >/dev/null
sudo chmod 600 "$PASSWORD_FILE"
if id 3pips >/dev/null 2>&1; then
  sudo chown 3pips:3pips "$PASSWORD_FILE"
fi

upsert_env() {
  local key="$1"
  local value="$2"
  sudo python3 - "$ENV_FILE" "$key" "$value" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]

if path.exists():
    lines = path.read_text(encoding="utf-8").splitlines()
else:
    lines = []

out = []
found = False
for line in lines:
    if line.startswith(f"{key}="):
        out.append(f"{key}={value}")
        found = True
    else:
        out.append(line)
if not found:
    out.append(f"{key}={value}")
path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
PY
}

upsert_env "ARCHIVE_EMAIL_ENABLED" "1"
upsert_env "ARCHIVE_EMAIL_TO" "$EMAIL"
upsert_env "ARCHIVE_EMAIL_FROM" "$EMAIL"
upsert_env "ARCHIVE_SMTP_HOST" "smtp.yandex.ru"
upsert_env "ARCHIVE_SMTP_PORT" "465"
upsert_env "ARCHIVE_SMTP_USER" "$EMAIL"
upsert_env "ARCHIVE_SMTP_PASSWORD_FILE" "$PASSWORD_FILE"
upsert_env "ARCHIVE_SMTP_USE_SSL" "1"
upsert_env "ARCHIVE_SMTP_STARTTLS" "0"
upsert_env "ARCHIVE_DAILY_TIME" "$DAILY_TIME"

upsert_env "WATCHDOG_EMAIL_ENABLED" "1"
upsert_env "WATCHDOG_EMAIL_TO" "$EMAIL"
upsert_env "WATCHDOG_EMAIL_FROM" "$EMAIL"
upsert_env "WATCHDOG_SMTP_HOST" "smtp.yandex.ru"
upsert_env "WATCHDOG_SMTP_PORT" "465"
upsert_env "WATCHDOG_SMTP_USER" "$EMAIL"
upsert_env "WATCHDOG_SMTP_PASSWORD_FILE" "$PASSWORD_FILE"
upsert_env "WATCHDOG_SMTP_USE_SSL" "1"
upsert_env "WATCHDOG_SMTP_STARTTLS" "0"

cd "$PROJECT_ROOT"
sudo docker compose --env-file "$ENV_FILE" up -d archive >/dev/null

if systemctl list-unit-files 2>/dev/null | grep -q '^3pips-watchdog.timer'; then
  sudo systemctl start 3pips-watchdog.service || true
fi

if [[ "$TEST_NOW" -eq 1 ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  sudo docker compose --env-file "$ENV_FILE" exec -T archive \
    python /app/scripts/archive_paper_run.py \
      --project-root /app \
      --run-name "${ARCHIVE_RUN_NAME:-v7_live_20260525}" \
      --keep-days "${ARCHIVE_KEEP_DAYS:-14}" \
      --daily-raw-day "$(date '+%F')"
fi

echo "Mail delivery enabled."
echo "Mailbox: $EMAIL"
echo "Daily raw archive time: $DAILY_TIME"
echo "Password file: $PASSWORD_FILE"
