#!/usr/bin/env bash
# Start (or restart) the huawei-reports-web container from the self-contained image.
# Run this on the target server after `docker load < huawei-reports-web.tar.gz`.
set -euo pipefail

CONTAINER="huawei-reports-web"
IMAGE="huawei-reports-web:latest"
DB_DIR="/home/liangzhu/huawei-db"

# Required env vars — set these before running, e.g. in a .env file or inline.
# For HPC auth, prefer an SSH key at $HPC_SSH_KEY_SRC. If the key is absent,
# the script falls back to password auth via $HPC_PASSWORD.
: "${ADMIN_EMAIL:?ADMIN_EMAIL must be set}"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD must be set}"

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

echo "==> Ensuring DB directory exists"
mkdir -p "$DB_DIR"

echo "==> Removing old container if present"
docker rm -f "$CONTAINER" 2>/dev/null || true

echo "==> Starting container $CONTAINER"
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
  -v "$DB_DIR":/opt/db-data \
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
echo "    Inspect auth log:     docker exec $CONTAINER tail -f /var/log/update/auth.log"
echo "    Query DB:             sqlite3 $DB_DIR/usage.sqlite"
