#!/usr/bin/env bash
# Build a portable Docker image and export it as a .tar.gz.
# The resulting artifact is self-contained — it includes HTML, nginx.conf,
# Python backends, and DB tooling.  Import on any server with:
#     docker load < huawei-reports-web.tar.gz
#
# Usage:
#   ./deploy/export-image.sh [OUTPUT_PATH]
#
# Default output: ./huawei-reports-web.tar.gz
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WEB_DIR="$REPO_ROOT/web"

TAG="huawei-reports-web:latest"
OUTPUT="${1:-$REPO_ROOT/huawei-reports-web.tar.gz}"

echo "==> Building self-contained image ($TAG)"
docker build -f "$WEB_DIR/Dockerfile.migrate" -t "$TAG" "$WEB_DIR"

echo "==> Exporting to $OUTPUT"
docker save "$TAG" | gzip > "$OUTPUT"

SIZE=$(du -sh "$OUTPUT" | cut -f1)
echo "==> Done. Image size: $SIZE"
echo ""
echo "    Copy to target server:"
echo "      scp $OUTPUT user@target:/tmp/"
echo ""
echo "    On target server:"
echo "      docker load < /tmp/$(basename "$OUTPUT")"
echo "      docker run -d --name huawei-reports-web --restart unless-stopped \\"
echo "        --network host \\"
echo "        -e HPC_PASSWORD=... -e ADMIN_EMAIL=... -e ADMIN_PASSWORD=... \\"
echo "        -v /path/to/db-data:/opt/db-data \\"
echo "        $TAG"
