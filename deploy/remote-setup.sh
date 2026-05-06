#!/usr/bin/env bash
set -euo pipefail

REPORTS_DIR="/home/liangzhu/huawei-reports"
WEB_DIR="/home/liangzhu/huawei-reports-web"
BUNDLE="/tmp/deploy-bundle.tgz"
CONTAINER="huawei-reports-web"

echo "==> Creating directories"
mkdir -p "$REPORTS_DIR/archive"
mkdir -p "$WEB_DIR"

echo "==> Extracting bundle"
if [ -f "$BUNDLE" ]; then
  tar xzf "$BUNDLE" -C /tmp/
  mv -f /tmp/docker-compose.yml "$WEB_DIR/" || true
  mv -f /tmp/nginx.conf "$WEB_DIR/"
  mv -f /tmp/index.html "$REPORTS_DIR/"
fi

echo "==> Files in place:"
ls -la "$WEB_DIR"
ls -la "$REPORTS_DIR"

echo "==> Removing old container if present"
docker rm -f "$CONTAINER" 2>/dev/null || true

echo "==> Starting container via docker run"
docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  -p 18788:80 \
  -v "$REPORTS_DIR":/usr/share/nginx/html:ro \
  -v "$WEB_DIR/nginx.conf":/etc/nginx/conf.d/default.conf:ro \
  nginx:alpine

echo "==> Container status"
docker ps --filter "name=$CONTAINER" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

echo "==> Health check"
sleep 2
curl -sI http://localhost:18788 | head -5
echo
echo "==> Done. Try http://10.26.15.53:18788 from your browser."
