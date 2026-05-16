#!/usr/bin/env bash
# Build the report site image (nginx + python + cron) and (re)start the container.
# Assumes the bundle has been scp'd to /tmp/deploy-bundle.tgz on claw and that
# HPC_PASSWORD is exported in the environment of this script.
set -euo pipefail

REPORTS_DIR="/home/liangzhu/huawei-reports"
WEB_SRC="/home/liangzhu/huawei-reports-web"
BUNDLE="/tmp/deploy-bundle.tgz"
CONTAINER="huawei-reports-web"
IMAGE="huawei-reports-web:latest"

: "${HPC_PASSWORD:?HPC_PASSWORD must be set in the env when running this script}"
UPDATE_CRON="${UPDATE_CRON:-0 3 * * *}"
DAYS="${DAYS:-30}"

echo "==> Preparing directories"
mkdir -p "$REPORTS_DIR/archive" "$REPORTS_DIR/data" "$WEB_SRC"

echo "==> Extracting bundle"
if [ -f "$BUNDLE" ]; then
  rm -rf "$WEB_SRC"/*
  tar xzf "$BUNDLE" -C "$WEB_SRC"
  # HTML pages live in the reports dir (served at /), not the build context.
  for html in index.html apply.html; do
    if [ -f "$WEB_SRC/$html" ]; then
      mv -f "$WEB_SRC/$html" "$REPORTS_DIR/$html"
    fi
  done
fi

echo "==> Build context:"
ls -la "$WEB_SRC"

echo "==> Building image $IMAGE"
docker build -t "$IMAGE" "$WEB_SRC"

echo "==> Removing old container if present"
docker rm -f "$CONTAINER" 2>/dev/null || true

echo "==> Starting container"
docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  --network host \
  -e HPC_PASSWORD="$HPC_PASSWORD" \
  -e UPDATE_CRON="$UPDATE_CRON" \
  -e DAYS="$DAYS" \
  -v "$REPORTS_DIR":/usr/share/nginx/html \
  -v "$WEB_SRC/nginx.conf":/etc/nginx/conf.d/default.conf:ro \
  "$IMAGE"

echo "==> Container status"
docker ps --filter "name=$CONTAINER" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

echo "==> Tailing first 20 lines of container log"
sleep 3
docker logs --tail 20 "$CONTAINER" || true

echo "==> Health check"
sleep 1
curl -sI http://localhost:18788/ | head -3 || true
echo

echo "==> Done. http://10.26.15.53:18788"
echo "    Inspect update log: docker exec $CONTAINER tail -f /var/log/update/update.log"
