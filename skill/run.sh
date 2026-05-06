#!/usr/bin/env bash
# Update the Huawei report site with fresh slurm core-hours data.
# Override DAYS via env var or secrets.env.
set -euo pipefail

SKILL_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -P "$SKILL_DIR/.." && pwd)"

# Load secrets if a secrets.env exists. Search the repo root and the skill dir.
for f in "$REPO_DIR/secrets.env" "$SKILL_DIR/secrets.env" "$HOME/.config/huawei-report/secrets.env"; do
  if [[ -r "$f" ]]; then
    # shellcheck disable=SC1090
    set -a; source "$f"; set +a
    echo "    loaded secrets from $f"
    break
  fi
done

if [[ -z "${HPC_PASSWORD:-}" || -z "${CLAW_PASSWORD:-}" ]]; then
  echo "ERROR: HPC_PASSWORD and CLAW_PASSWORD must be set (via secrets.env or env)." >&2
  exit 1
fi

DAYS="${DAYS:-30}"
SCRATCH="$HOME/Downloads/huawei-report"
mkdir -p "$SCRATCH"

RAW="$SCRATCH/sacct_raw.tsv"
JSON="$SCRATCH/slurm_daily.json"

echo "==> [1/3] Pulling sacct from hpc (last ${DAYS}d)"
HPC_PASSWORD="$HPC_PASSWORD" expect "$SKILL_DIR/pull-sacct.exp" "$DAYS" "$RAW"
echo "    wrote $RAW ($(wc -l < "$RAW") lines)"

echo "==> [2/3] Aggregating per-user per-day core-hours"
python3 "$SKILL_DIR/collect_slurm.py" "$RAW" "$DAYS" "$JSON"

echo "==> [3/3] Pushing to claw"
CLAW_PASSWORD="$CLAW_PASSWORD" expect "$SKILL_DIR/push-json.exp" "$JSON"

echo
echo "Done. Refresh http://10.26.15.53:18788 to see the chart."
