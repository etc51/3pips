#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 user@SERVER_IP [PROJECT_ROOT]"
  exit 1
fi

SERVER="$1"
PROJECT_ROOT="${2:-/opt/3pips}"

find_token() {
  if [[ -n "${TBANK_TOKEN:-}" ]]; then
    printf '%s' "$TBANK_TOKEN"
    return 0
  fi
  if [[ -n "${TBANK_TOKEN_READONLY:-}" ]]; then
    printf '%s' "$TBANK_TOKEN_READONLY"
    return 0
  fi
  if [[ -n "${TINKOFF_TOKEN:-}" ]]; then
    printf '%s' "$TINKOFF_TOKEN"
    return 0
  fi
  if [[ -d "$HOME/Desktop" ]]; then
    grep -Eoh 't\.[A-Za-z0-9_-]{20,}|[A-Za-z0-9_-]{40,}' "$HOME"/Desktop/*.txt 2>/dev/null | head -n 1
    return 0
  fi
  return 1
}

TOKEN="$(find_token || true)"
if [[ -z "$TOKEN" ]]; then
  echo "T-Bank token not found in env or Desktop txt files."
  exit 1
fi

printf '%s' "$TOKEN" | ssh "$SERVER" "mkdir -p '$PROJECT_ROOT/secrets' && umask 077 && cat > '$PROJECT_ROOT/secrets/tbank_token.txt' && cd '$PROJECT_ROOT' && sudo docker compose restart paper"
echo "Token pushed to $SERVER:$PROJECT_ROOT/secrets/tbank_token.txt and paper container restarted."
