#!/bin/bash
set -euo pipefail

REPO_DIR="/Users/woongbaean/chzzk-discord-watcher"
GH="/usr/local/bin/gh"
LOCK_DIR="/tmp/chzzk-discord-watcher-trigger.lock"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "$(date -u +"%Y-%m-%dT%H:%M:%SZ") trigger skipped: previous run still active"
  exit 0
fi
trap 'rmdir "$LOCK_DIR"' EXIT

cd "$REPO_DIR"

echo "$(date -u +"%Y-%m-%dT%H:%M:%SZ") trigger start"
"$GH" workflow run "Chzzk Watcher" \
  --repo HelloBEATCoin/chzzk-discord-watcher \
  --ref main
echo "$(date -u +"%Y-%m-%dT%H:%M:%SZ") trigger queued"
