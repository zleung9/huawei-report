#!/bin/sh
# One-shot backfill: pull last N days of sacct (default 180) and write to
# hpc_daily. Idempotent — re-running just refreshes the rows.
#
# Env: HPC_HOST, HPC_USER, HPC_SSH_KEY or HPC_PASSWORD, DB_PATH,
# BACKFILL_DAYS.
set -eu

: "${HPC_HOST:=172.16.12.2}"
: "${HPC_USER:=lz}"
: "${DB_PATH:=/opt/db-data/usage.sqlite}"
: "${BACKFILL_DAYS:=180}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCHEMA="$SCRIPT_DIR/schema.sql"
INIT="$SCRIPT_DIR/init_db.py"
AGG="$SCRIPT_DIR/aggregate_hpc.py"

mkdir -p "$(dirname "$DB_PATH")"

RAW=$(mktemp)
trap 'rm -f "$RAW"' EXIT

ts() { date +"%Y-%m-%dT%H:%M:%S%z"; }
echo "[$(ts)] backfill: pulling ${BACKFILL_DAYS}d of sacct from ${HPC_USER}@${HPC_HOST}"

SACCT_CMD="sacct -aX --starttime=now-${BACKFILL_DAYS}days --format=User,Account,JobID,State,CPUTimeRAW,Start,End,AllocCPUS,Elapsed -P"

if [ -n "${HPC_SSH_KEY:-}" ]; then
  if [ ! -r "$HPC_SSH_KEY" ]; then
    echo "[$(ts)] ERROR: HPC_SSH_KEY is set but not readable: $HPC_SSH_KEY" >&2
    exit 1
  fi
  echo "[$(ts)] auth: using SSH key $HPC_SSH_KEY"
  ssh \
    -i "$HPC_SSH_KEY" \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR \
    -o ConnectTimeout=20 \
    -o ServerAliveInterval=15 \
    -o ServerAliveCountMax=2 \
    -p 22 \
    "${HPC_USER}@${HPC_HOST}" \
    "$SACCT_CMD" \
    > "$RAW"
else
  : "${HPC_PASSWORD:?HPC_PASSWORD required when HPC_SSH_KEY is not set}"
  echo "[$(ts)] auth: using password"
  sshpass -p "$HPC_PASSWORD" ssh \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR \
    -o ConnectTimeout=20 \
    -o ServerAliveInterval=15 \
    -o ServerAliveCountMax=2 \
    -o NumberOfPasswordPrompts=1 \
    -p 22 \
    "${HPC_USER}@${HPC_HOST}" \
    "$SACCT_CMD" \
    > "$RAW"
fi

echo "[$(ts)] sacct: $(wc -l < "$RAW") lines"

echo "[$(ts)] ensuring DB exists and seeding person/accounts/groups"
python3 "$INIT" "$DB_PATH" "$SCHEMA" "$RAW"

echo "[$(ts)] aggregating into hpc_daily (window ${BACKFILL_DAYS}d)"
python3 "$AGG" "$RAW" "$BACKFILL_DAYS" "$DB_PATH"

echo "[$(ts)] done"
