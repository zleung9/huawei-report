#!/usr/bin/env bash
# Symlink the repo's skill/ into Claude Code's skill discovery path.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="$HOME/.claude/skills/huawei-report-update"

mkdir -p "$HOME/.claude/skills"
if [[ -e "$TARGET" && ! -L "$TARGET" ]]; then
  echo "ERROR: $TARGET exists and is not a symlink. Move or remove it first." >&2
  exit 1
fi
rm -f "$TARGET"
ln -s "$REPO_DIR/skill" "$TARGET"
chmod +x "$REPO_DIR/skill/"*.sh "$REPO_DIR/skill/"*.exp "$REPO_DIR/skill/"*.py 2>/dev/null || true
echo "Linked $TARGET -> $REPO_DIR/skill"
echo
echo "Next: copy secrets.env.example to secrets.env at the repo root and fill in passwords."
