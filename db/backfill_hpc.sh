#!/bin/sh
# One-shot backfill: pull last N days of sacct (default 180) and write to
# hpc_daily. Idempotent — re-running just refreshes the rows.
#
# Env: HPC_HOST, HPC_USER, HPC_PASSWORD, DB_PATH, BACKFILL_DAYS.
set -eu

: "${HPC_HOST:=172.16.12.2}"
: "${HPC_USER:=lz}"
: "${HPC_PASSWORD:?HPC_PASSWORD required}"
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

sshpass -p "$HPC_PASSWORD" ssh \
  -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null \
  -o LogLevel=ERROR \
  -p 22 \
  "${HPC_USER}@${HPC_HOST}" \
  "sacct -aX --starttime=now-${BACKFILL_DAYS}days --format=User,Account,JobID,State,CPUTimeRAW,Start,End,AllocCPUS,Elapsed -P" \
  > "$RAW"

echo "[$(ts)] sacct: $(wc -l < "$RAW") lines"

echo "[$(ts)] ensuring DB exists and seeding person/accounts/groups"
python3 "$INIT" "$DB_PATH" "$SCHEMA" "$RAW"

echo "[$(ts)] aggregating into hpc_daily (window ${BACKFILL_DAYS}d)"
python3 "$AGG" "$RAW" "$BACKFILL_DAYS" "$DB_PATH"

echo "[$(ts)] done"
