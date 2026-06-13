#!/usr/bin/env sh
set -eu

PROJECT_ROOT="${PROJECT_ROOT:-/app}"
RUN_NAME="${ARCHIVE_RUN_NAME:-v7_live_20260525}"
DAILY_TIME="${ARCHIVE_DAILY_TIME:-23:59}"
STATE_PATH="${ARCHIVE_LOOP_STATE_PATH:-${PROJECT_ROOT}/reports/runtime/docker_archive_loop_state.json}"
CHECK_SEC="${ARCHIVE_LOOP_CHECK_SEC:-30}"
KEEP_DAYS="${ARCHIVE_KEEP_DAYS:-14}"

mkdir -p "$(dirname "$STATE_PATH")"

last_day() {
  if [ -f "$STATE_PATH" ]; then
    grep -Eo '"last_sent_day"[[:space:]]*:[[:space:]]*"[^"]+"' "$STATE_PATH" | sed -E 's/.*"([^"]+)"/\1/' || true
  fi
}

write_state() {
  day="$1"
  ts="$(date '+%F %T')"
  cat >"$STATE_PATH" <<EOF
{
  "last_sent_day": "$day",
  "updated_at": "$ts"
}
EOF
}

while true; do
  today="$(date '+%F')"
  now_hm="$(date '+%H:%M')"
  sent_day="$(last_day)"

  if [ "$now_hm" \> "$DAILY_TIME" ] || [ "$now_hm" = "$DAILY_TIME" ]; then
    if [ "$sent_day" != "$today" ]; then
      python "$PROJECT_ROOT/scripts/archive_paper_run.py" \
        --project-root "$PROJECT_ROOT" \
        --run-name "$RUN_NAME" \
        --keep-days "$KEEP_DAYS" \
        --daily-raw-day "$today"
      write_state "$today"
      sleep 65
      continue
    fi
  fi

  sleep "$CHECK_SEC"
done
