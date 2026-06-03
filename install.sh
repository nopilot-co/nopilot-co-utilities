#!/usr/bin/env bash
# Install the nopilot-co-utilities marketplace + all utility plugins.
#
# This script:
#   1. Registers this repo as a Claude Code plugin marketplace
#      (via the .claude-plugin/marketplace.json manifest).
#   2. Installs every utility plugin listed below from that marketplace.
#   3. Runs each utility's own install.sh so its standalone CLI + deps are
#      available independently of the plugin.
#
# Idempotent: safe to re-run. Usage: ./install.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MARKETPLACE="nopilot-co-utilities"

# Each utility lives in a top-level directory whose name matches its plugin.
PLUGINS=("youtube-transcript" "notion-sources")

# ---------------------------------------------------------------- 1. marketplace
echo "== marketplace =="
if ! command -v claude > /dev/null 2>&1; then
  echo "  • claude CLI not on PATH — skipping plugin install."
  echo "    (manual: claude plugin marketplace add $ROOT)"
else
  if claude plugin marketplace add "$ROOT" 2>&1 | grep -qiE "(Successfully added|already)"; then
    echo "  ✓ marketplace registered"
  elif claude plugin marketplace list 2> /dev/null | grep -q "$MARKETPLACE"; then
    echo "  ✓ marketplace already registered"
  else
    echo "  ! could not register marketplace — add manually: claude plugin marketplace add $ROOT"
  fi

  # ------------------------------------------------------------- 2. plugins
  echo "== plugins =="
  for name in "${PLUGINS[@]}"; do
    if claude plugin install "${name}@${MARKETPLACE}" 2>&1 | grep -qiE "(Successfully installed|already|updated)"; then
      echo "  ✓ ${name}"
    else
      echo "  ! ${name} — install manually: claude plugin install ${name}@${MARKETPLACE}"
    fi
  done
fi

# ---------------------------------------------------------------- 3. standalone CLIs
echo "== standalone CLIs =="
for name in "${PLUGINS[@]}"; do
  if [ -x "$ROOT/$name/install.sh" ]; then
    "$ROOT/$name/install.sh" || echo "  ! $name/install.sh reported a problem"
  fi
done

echo "Done."
