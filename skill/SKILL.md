---
name: huawei-report-update
description: Refresh the Huawei server weekly report site at http://10.26.15.53:18788. Pulls slurm job records from hpc (10.26.15.51), aggregates per-user per-day core-hours over the last 30 days, and pushes JSON to the report folder so the frontend (Chart.js) renders the chart. Use when user says 更新华为周报 / 刷新报告 / huawei report update / 拉slurm数据 / 更新核时图.
---

# huawei-report-update

When invoked, run the full update pipeline:

```bash
bash ~/.claude/skills/huawei-report-update/run.sh
```

The pipeline:

1. SSH into `hpc` (10.26.15.51) via `expect` and run `sacct` for the past 30 days.
2. Parse the records locally with `collect_slurm.py` — for each (user, day) bucket the per-job overlap × AllocCPUS / 3600 to get fair daily core-hours even for still-running jobs.
3. Write `slurm_daily.json` locally to `~/Downloads/huawei-report/`.
4. SCP the JSON to `claw:/home/liangzhu/huawei-reports/data/slurm_daily.json`.
5. nginx (already running on the huawei server) serves it; the front page fetches it and re-renders.

Files in this skill:
- `run.sh` — orchestrator (sources `secrets.env` for HPC/CLAW passwords)
- `collect_slurm.py` — parse sacct output → JSON
- `pull-sacct.exp` — expect wrapper for ssh hpc (reads `$HPC_PASSWORD`)
- `push-json.exp` — expect wrapper for scp to claw (reads `$CLAW_PASSWORD`)

Index page (`/home/liangzhu/huawei-reports/index.html`) was patched once to embed Chart.js and a `<canvas>` that fetches `data/slurm_daily.json`. Re-running the skill only refreshes the JSON; no need to touch the HTML again unless the chart layout changes.

## Tuning the time window

Default range is the last 30 days. Override with the env var:

```bash
DAYS=60 bash ~/.claude/skills/huawei-report-update/run.sh
```

## Setup

This skill requires a `secrets.env` file at the repo root (the directory containing this skill folder). See `secrets.env.example` for the template.

## Notes

- Long-running jobs without an End time are clipped at "now" for the daily attribution — re-running the skill the next day will retroactively re-attribute those hours correctly.
- Replace password auth with SSH keys when convenient — the expect scripts only exist as a workaround.
