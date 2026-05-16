#!/bin/sh
# Pull sacct from HPC, aggregate, atomic-write to OUT_DIR/slurm_daily.json.
# Env: HPC_HOST, HPC_USER, HPC_PASSWORD, DAYS, OUT_DIR.
set -eu

: "${HPC_HOST:?HPC_HOST required}"
: "${HPC_USER:?HPC_USER required}"
: "${HPC_PASSWORD:?HPC_PASSWORD required}"
: "${DAYS:=30}"
: "${OUT_DIR:=/usr/share/nginx/html/data}"

mkdir -p "$OUT_DIR"
RAW=$(mktemp)
trap 'rm -f "$RAW"' EXIT

ts() { date +"%Y-%m-%dT%H:%M:%S%z"; }
echo "[$(ts)] pulling sacct from ${HPC_USER}@${HPC_HOST} (last ${DAYS}d)"

sshpass -p "$HPC_PASSWORD" ssh \
  -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null \
  -o LogLevel=ERROR \
  -p 22 \
  "${HPC_USER}@${HPC_HOST}" \
  "sacct -aX --starttime=now-${DAYS}days --format=User,Account,JobID,State,CPUTimeRAW,Start,End,AllocCPUS,Elapsed -P" \
  > "$RAW"

LINES=$(wc -l < "$RAW")
echo "[$(ts)] sacct: $LINES lines"

TMP="${OUT_DIR}/slurm_daily.json.tmp"
python3 /opt/update/collect_slurm.py "$RAW" "$DAYS" "$TMP"
mv -f "$TMP" "${OUT_DIR}/slurm_daily.json"
echo "[$(ts)] wrote ${OUT_DIR}/slurm_daily.json"
