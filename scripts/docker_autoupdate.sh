#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/opt/3pips}"
REMOTE_NAME="${AUTOUPDATE_REMOTE:-origin}"
BRANCH_NAME="${AUTOUPDATE_BRANCH:-main}"
REQUIRE_IDLE="${AUTOUPDATE_REQUIRE_IDLE:-1}"
RUN_NAME="${AUTOUPDATE_RUN_NAME:-v7_live_20260525}"
LOG_PATH="${AUTOUPDATE_LOG_PATH:-${PROJECT_ROOT}/reports/runtime/docker_autoupdate.log}"
STATE_PATH="${AUTOUPDATE_STATE_PATH:-${PROJECT_ROOT}/reports/runtime/docker_autoupdate_state.json}"
LOCK_PATH="${AUTOUPDATE_LOCK_PATH:-${PROJECT_ROOT}/reports/runtime/docker_autoupdate.lock}"

mkdir -p "$(dirname "$LOG_PATH")"
mkdir -p "$(dirname "$STATE_PATH")"

exec 9>"$LOCK_PATH"
if ! flock -n 9; then
  echo "[$(date '+%F %T')] skip reason=lock_held" >>"$LOG_PATH"
  exit 0
fi

cd "$PROJECT_ROOT"

log() {
  echo "[$(date '+%F %T')] $*" | tee -a "$LOG_PATH"
}

open_positions_total() {
  python3 - "$PROJECT_ROOT" "$RUN_NAME" <<'PY'
from __future__ import annotations
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
run_name = sys.argv[2]
run_dir = root / "reports" / "paper_runs" / run_name
total = 0
if run_dir.exists():
    for path in run_dir.glob("*_paper_open_positions.json"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
            if not text:
                continue
            payload = json.loads(text)
        except Exception:
            continue
        if isinstance(payload, list):
            total += len([item for item in payload if isinstance(item, dict) and item])
        elif isinstance(payload, dict):
            positions = payload.get("positions", [])
            if isinstance(positions, list):
                total += len([item for item in positions if isinstance(item, dict) and item])
print(total)
PY
}

current_head="$(git rev-parse HEAD)"
git fetch --quiet "$REMOTE_NAME" "$BRANCH_NAME"
remote_head="$(git rev-parse "${REMOTE_NAME}/${BRANCH_NAME}")"

if [[ "$current_head" == "$remote_head" ]]; then
  log "skip reason=up_to_date head=$current_head"
  exit 0
fi

if [[ -n "$(git status --porcelain)" ]]; then
  log "skip reason=dirty_worktree current=$current_head remote=$remote_head"
  exit 0
fi

if [[ "$REQUIRE_IDLE" == "1" ]]; then
  open_count="$(open_positions_total)"
  if [[ "$open_count" != "0" ]]; then
    log "skip reason=open_positions count=$open_count current=$current_head remote=$remote_head"
    exit 0
  fi
fi

log "update_start current=$current_head remote=$remote_head"
git pull --ff-only "$REMOTE_NAME" "$BRANCH_NAME"
docker compose up -d --build

python3 - "$STATE_PATH" "$current_head" "$remote_head" <<'PY'
from __future__ import annotations
import json
import sys
from datetime import datetime
from pathlib import Path

state_path = Path(sys.argv[1])
previous_head = sys.argv[2]
new_head = sys.argv[3]
payload = {
    "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "previous_head": previous_head,
    "current_head": new_head,
}
state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
PY

log "update_done current=$remote_head"
