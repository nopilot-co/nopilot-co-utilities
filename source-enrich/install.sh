#!/usr/bin/env bash
# Install the source-enrich standalone CLI + its Python dependencies.
#
# This script:
#   1. Installs the extraction dependencies (trafilatura + PyYAML).
#   2. Exposes the enricher as a standalone command `source-enrich` on PATH
#      (symlinked into ~/.local/bin), so the utility is runnable on its own —
#      independent of the Claude Code plugin.
#
# Idempotent: safe to re-run. Usage: ./install.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/scripts/enrich.py"
BIN_DIR="${SOURCE_ENRICH_BIN_DIR:-$HOME/.local/bin}"
CMD="$BIN_DIR/source-enrich"

echo "source-enrich — install"

# ----------------------------------------------------------- 1. python + deps
if ! command -v python3 > /dev/null 2>&1; then
  echo "  ! python3 not on PATH — install Python ≥ 3.8 first (brew install python@3.12)"
  exit 1
fi

echo "  • installing extraction deps (trafilatura + PyYAML + lxml_html_clean + pypdf)…"
python3 -m pip install --quiet --upgrade trafilatura PyYAML lxml_html_clean pypdf

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
  done. Enrich a notion-sources batch:
    source-enrich --batch path/to/research/sources --limit 5
  (YouTube sources also use yt-transcript if youtube-transcript is installed.)
EOF
