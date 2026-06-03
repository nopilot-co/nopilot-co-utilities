#!/usr/bin/env bash
# Install the notion-sources standalone CLI.
#
# This script:
#   1. Verifies Python 3 is available. notion-sources has NO third-party deps —
#      it uses only the standard library — so there is nothing to pip install.
#   2. Exposes the extractor as a standalone command `notion-sources` on PATH
#      (symlinked into ~/.local/bin), so the utility is runnable on its own —
#      independent of the Claude Code plugin.
#
# Idempotent: safe to re-run. Usage: ./install.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/scripts/extract.py"
BIN_DIR="${NOTION_SOURCES_BIN_DIR:-$HOME/.local/bin}"
CMD="$BIN_DIR/notion-sources"

echo "notion-sources — install"

# ----------------------------------------------------------- 1. python
if ! command -v python3 > /dev/null 2>&1; then
  echo "  ! python3 not on PATH — install Python ≥ 3.8 first (brew install python@3.12)"
  exit 1
fi
echo "  • no third-party dependencies — standard library only"

# ----------------------------------------------------------- 2. standalone CLI
chmod +x "$SCRIPT"
mkdir -p "$BIN_DIR"
ln -sf "$SCRIPT" "$CMD"
echo "  ✓ linked standalone CLI: $CMD -> $SCRIPT"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "  ! $BIN_DIR is not on PATH — add it, e.g.: export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac

cat <<'EOF'
  done. Set credentials, then run:
    export NOPILOT_NOTION_API_KEY=ntn_...
    export NOPILOT_NOTION_SOURCE_DATABASE_ID=...
    notion-sources --out sources/
  (or pass --token / --database, or point --env-file at a .env)
EOF
