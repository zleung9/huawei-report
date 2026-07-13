#!/usr/bin/env bash
# Build the report site image (nginx + python + cron) and (re)start the container.
# Assumes the bundle has been scp'd to /tmp/deploy-bundle.tgz on claw and that
# HPC_PASSWORD is exported in the environment of this script.
set -euo pipefail

REPORTS_DIR="/home/liangzhu/huawei-reports"
WEB_SRC="/home/liangzhu/huawei-reports-web"
DB_DIR="/home/liangzhu/huawei-db"
BUNDLE="/tmp/deploy-bundle.tgz"
CONTAINER="huawei-reports-web"
IMAGE="huawei-reports-web:latest"

# For HPC auth, prefer an SSH key at $HPC_SSH_KEY_SRC. If the key is absent,
# the script falls back to password auth via $HPC_PASSWORD.
: "${ADMIN_EMAIL:?ADMIN_EMAIL must be set in the env when running this script}"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD must be set in the env when running this script}"
UPDATE_CRON="${UPDATE_CRON:-0 3 * * *}"
DAYS="${DAYS:-30}"
BACKFILL_DAYS="${BACKFILL_DAYS:-180}"
HPC_SSH_KEY_SRC="${HPC_SSH_KEY_SRC:-/home/liangzhu/huawei-ssh/hpc_lz_ed25519}"
HPC_SSH_KEY_DST="${HPC_SSH_KEY_DST:-/opt/ssh/id_ed25519}"

HPC_AUTH_ARGS=()
if [[ -f "$HPC_SSH_KEY_SRC" ]]; then
  echo "==> Using HPC SSH key: $HPC_SSH_KEY_SRC"
  HPC_AUTH_ARGS=(
    -e "HPC_SSH_KEY=$HPC_SSH_KEY_DST"
    -v "$HPC_SSH_KEY_SRC:$HPC_SSH_KEY_DST:ro"
  )
else
  : "${HPC_PASSWORD:?HPC_PASSWORD must be set when HPC_SSH_KEY_SRC is not present}"
  echo "==> Using HPC password auth (HPC_SSH_KEY_SRC not found: $HPC_SSH_KEY_SRC)"
  HPC_AUTH_ARGS=(-e "HPC_PASSWORD=$HPC_PASSWORD")
fi

echo "==> Preparing directories"
mkdir -p "$REPORTS_DIR/archive" "$REPORTS_DIR/data" "$WEB_SRC" "$DB_DIR/backups"

echo "==> Extracting bundle"
if [ -f "$BUNDLE" ]; then
  rm -rf "$WEB_SRC"/*
  tar xzf "$BUNDLE" -C "$WEB_SRC"
  # HTML pages live in the reports dir (served at /), not the build context.
  for html in index.html apply.html detail.html login.html register.html profile.html; do
    if [ -f "$WEB_SRC/$html" ]; then
      mv -f "$WEB_SRC/$html" "$REPORTS_DIR/$html"
    fi
  done
fi

echo "==> Build context:"
ls -la "$WEB_SRC"

echo "==> Building image $IMAGE"
if [ -f "$WEB_SRC/Dockerfile.migrate" ]; then
  docker build -f "$WEB_SRC/Dockerfile.migrate" -t "$IMAGE" "$WEB_SRC"
else
  docker build -t "$IMAGE" "$WEB_SRC"
fi

echo "==> Removing old container if present"
docker rm -f "$CONTAINER" 2>/dev/null || true

echo "==> Starting container"
docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  --network host \
  "${HPC_AUTH_ARGS[@]}" \
  -e ADMIN_EMAIL="$ADMIN_EMAIL" \
  -e ADMIN_PASSWORD="$ADMIN_PASSWORD" \
  -e UPDATE_CRON="$UPDATE_CRON" \
  -e DAYS="$DAYS" \
  -e BACKFILL_DAYS="$BACKFILL_DAYS" \
  -v "$REPORTS_DIR":/usr/share/nginx/html \
  -v "$DB_DIR":/opt/db-data \
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
echo "    Inspect update log:   docker exec $CONTAINER tail -f /var/log/update/update.log"
echo "    Inspect backfill log: docker exec $CONTAINER tail -f /var/log/update/backfill.log"
echo "    Query DB:             sqlite3 $DB_DIR/usage.sqlite"
