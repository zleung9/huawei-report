#!/usr/bin/env bash
# Build, export, and upload the Docker image as a GitHub Release artifact.
# This keeps the binary out of git history while making it downloadable.
#
# Prerequisites:
#   - gh CLI installed and authenticated
#   - Docker running locally
#
# Usage:
#   ./deploy/release-image.sh [VERSION_TAG]
#
# Default tag: latest → creates/updates a "latest" release
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TAG="${1:-latest}"

# Determine git remote (prefer github)
REPO_SLUG=$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null \
  | sed -E 's|.*github.com[:/]([^/]+/[^/]+)(\.git)?|\1|' || true)
if [ -z "$REPO_SLUG" ]; then
  echo "ERROR: cannot determine GitHub repo slug from origin remote" >&2
  exit 1
fi

# Build + export
echo "==> Building self-contained image"
bash "$SCRIPT_DIR/export-image.sh" "$REPO_ROOT/huawei-reports-web.tar.gz"

SIZE=$(du -sh "$REPO_ROOT/huawei-reports-web.tar.gz" | cut -f1)
echo "==> Image size: $SIZE"

# Create or update GitHub release
echo "==> Uploading to GitHub release '$TAG' on $REPO_SLUG"
gh release create "$TAG" "$REPO_ROOT/huawei-reports-web.tar.gz" \
  --repo "$REPO_SLUG" \
  --title "huawei-reports-web $TAG" \
  --notes "Self-contained Docker image. Deploy with:
\`\`\`bash
# Download
gh release download $TAG --repo $REPO_SLUG
# Load
docker load < huawei-reports-web.tar.gz
# Start
HPC_PASSWORD=... ADMIN_EMAIL=... ADMIN_PASSWORD=... bash deploy/start-container.sh
\`\`\`
" \
  --clobber

echo "==> Done. Download URL:"
echo "    https://github.com/$REPO_SLUG/releases/tag/$TAG"
