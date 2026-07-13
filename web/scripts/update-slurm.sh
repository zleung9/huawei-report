#!/bin/sh
# Pull sacct from HPC, aggregate, atomic-write to OUT_DIR/slurm_daily.json,
# and UPSERT into hpc_daily in $DB_PATH (best-effort; failure is logged but
# does not break the JSON output).
# Env: HPC_HOST, HPC_USER, HPC_SSH_KEY or HPC_PASSWORD, DAYS, OUT_DIR,
# DB_PATH.
set -eu

: "${HPC_HOST:?HPC_HOST required}"
: "${HPC_USER:?HPC_USER required}"
: "${DAYS:=30}"
: "${OUT_DIR:=/usr/share/nginx/html/data}"
: "${DB_PATH:=/opt/db-data/usage.sqlite}"

mkdir -p "$OUT_DIR"
RAW=$(mktemp)
trap 'rm -f "$RAW"' EXIT

ts() { date +"%Y-%m-%dT%H:%M:%S%z"; }
echo "[$(ts)] pulling sacct from ${HPC_USER}@${HPC_HOST} (last ${DAYS}d)"

SACCT_CMD="sacct -aX --starttime=now-${DAYS}days --format=User,Account,JobID,State,CPUTimeRAW,Start,End,AllocCPUS,Elapsed -P"

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

LINES=$(wc -l < "$RAW")
echo "[$(ts)] sacct: $LINES lines"

TMP="${OUT_DIR}/slurm_daily.json.tmp"
python3 /opt/update/collect_slurm.py "$RAW" "$DAYS" "$TMP"
mv -f "$TMP" "${OUT_DIR}/slurm_daily.json"
echo "[$(ts)] wrote ${OUT_DIR}/slurm_daily.json"

# UPSERT recent window into hpc_daily (best-effort). The DB must already
# exist (run db/backfill_hpc.sh once after first deploy).
if [ -f "$DB_PATH" ]; then
    if python3 /opt/dbtools/aggregate_hpc.py "$RAW" "$DAYS" "$DB_PATH"; then
        echo "[$(ts)] hpc_daily upsert ok"
    else
        echo "[$(ts)] WARN: hpc_daily upsert failed (continuing)"
    fi
else
    echo "[$(ts)] WARN: $DB_PATH not found; skipping DB upsert (run backfill_hpc.sh once to create it)"
fi
