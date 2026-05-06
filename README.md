# Huawei Report

A self-updating report site hosted on the lab's Huawei server (10.26.15.53).
An OpenClaw-driven Claude Code skill pulls metrics from the HPC cluster and other sources, writes JSON to a folder served by an nginx Docker container, and the front page renders charts dynamically with Chart.js.

> Public URL (lab network): http://10.26.15.53:18788

## Architecture

```
┌──────────────────┐     ssh + sacct       ┌──────────────────────────┐
│  hpc cluster     │ ◀────────────────────▶│  Local Mac / openclaw-lz │
│  10.26.15.51     │                       │   skill: huawei-report-  │
│  (slurm)         │                       │            update         │
└──────────────────┘                       └────────────┬─────────────┘
                                                        │ scp JSON
                                                        ▼
                                       ┌──────────────────────────────┐
                                       │  huawei server 10.26.15.53   │
                                       │   /home/liangzhu/huawei-     │
                                       │   reports/                   │
                                       │     ├── index.html (Chart.js)│
                                       │     ├── data/*.json          │
                                       │     └── archive/*.html       │
                                       │  ↑ mounted read-only into    │
                                       │  ↑ docker container          │
                                       │   "huawei-reports-web"       │
                                       │   (nginx:alpine, port 18788) │
                                       └──────────────────────────────┘
                                                        │
                                                        ▼
                                                 [users' browsers]
```

Two clean halves:

1. **Web container** — passive. Just nginx serving a folder. The agent never touches the container; it only writes files into the mounted directory.
2. **Skill** — active. Refreshes the data files on demand (or on a schedule, future work).

## Repo layout

```
huawei-report/
├── README.md                    # this file
├── secrets.env.example          # template for HPC + claw passwords
├── .gitignore                   # excludes secrets.env, scratch artifacts
│
├── web/                         # web container side (deploy once)
│   ├── docker-compose.yml       # kept for reference; we currently use docker run
│   ├── nginx.conf               # autoindex on, utf-8, no-cache
│   └── index.html               # Chart.js front page
│
├── skill/                       # Claude Code skill (run repeatedly)
│   ├── SKILL.md                 # skill manifest
│   ├── run.sh                   # orchestrator
│   ├── collect_slurm.py         # parse sacct → per-user/per-day JSON
│   ├── pull-sacct.exp           # ssh hpc (reads $HPC_PASSWORD)
│   └── push-json.exp            # scp claw  (reads $CLAW_PASSWORD)
│
└── deploy/                      # one-shot setup helpers
    ├── remote-setup.sh          # ran on huawei server, started the nginx container
    └── install-skill.sh         # symlinks skill/ into ~/.claude/skills/
```

## One-time setup

### 1. Web container on the huawei server

`docker compose` plugin v2 isn't available there and the bundled `docker-compose v2.4.1` is too old for Docker 29.2.1, so we use plain `docker run`.

Files needed on the host (`10.26.15.53`):
- `/home/liangzhu/huawei-reports/index.html` (from `web/index.html`)
- `/home/liangzhu/huawei-reports-web/nginx.conf` (from `web/nginx.conf`)

Then run `deploy/remote-setup.sh` on the host. It creates dirs, removes any old container, and starts:

```bash
docker run -d \
  --name huawei-reports-web --restart unless-stopped \
  -p 18788:80 \
  -v /home/liangzhu/huawei-reports:/usr/share/nginx/html:ro \
  -v /home/liangzhu/huawei-reports-web/nginx.conf:/etc/nginx/conf.d/default.conf:ro \
  nginx:alpine
```

The mount is **read-only** for nginx, so reports can only be written by the skill side, never overwritten by mistake from inside the container.

### 2. Skill on the local Mac (or inside `openclaw-lz`)

```bash
cp secrets.env.example secrets.env
# edit secrets.env to fill HPC_PASSWORD and CLAW_PASSWORD

bash deploy/install-skill.sh    # symlinks skill/ into ~/.claude/skills/huawei-report-update
```

After this Claude Code auto-discovers the skill (`huawei-report-update`).

## Running the update

Either via Claude:

> 更新华为周报

Or directly:

```bash
bash ~/workspace/huawei-report/skill/run.sh
DAYS=60 bash ~/workspace/huawei-report/skill/run.sh   # custom window
```

The pipeline:

1. `expect ssh hpc` → runs `sacct -aX --starttime=now-${DAYS}days …` and dumps pipe-delimited rows.
2. `collect_slurm.py` parses the rows. For each job it computes the overlap of `[Start, min(End, now)]` with each calendar day, multiplies by `AllocCPUS`, divides by 3600 — fair daily attribution even when jobs span days or are still running.
3. JSON is scped to `claw:/home/liangzhu/huawei-reports/data/slurm_daily.json`.
4. nginx already serves it; the front page fetches it and re-renders.

Output JSON shape:

```json
{
  "generated_at": "2026-05-06T01:01:44+00:00",
  "range": { "start": "2026-04-07", "end": "2026-05-06", "days": 30 },
  "users": ["xmu_user02", "xmu_user04", ...],
  "dates": ["2026-04-07", ...],
  "core_hours": { "xmu_user04": [12.3, 8.0, ...], ... },
  "totals_per_day": [12.3, 20.3, ...],
  "totals_per_user": { "xmu_user04": 1081363.5, ... }
}
```

## Front-end

`web/index.html` is a single static page:

- Header — title + subtitle.
- HPC card — 4 stat tiles (window, total core-hours, active users, last update), a stacked bar chart (one series per user) and a leaderboard table.
- Archive section — placeholder for future weekly HTML reports under `/archive/`.

Chart.js is loaded from `cdn.jsdelivr.net`. Switch to a vendored copy if the lab restricts outbound traffic.

## Roadmap

- [x] Daily core-hours chart from slurm.
- [ ] Weekly snapshot under `archive/YYYY-Www.html`.
- [ ] LLM usage panel (Claude / Qwen / etc. — once a usage source exists).
- [ ] Disk + memory utilisation per node.
- [ ] Cron / systemd timer inside `openclaw-lz` so updates run automatically.
- [ ] Replace expect/password auth with SSH keys.

## Known issues

- `docker-compose v2.4.1` on the huawei host is too old for Docker 29.2.1's API. Using `docker run` sidesteps it. Upgrade plan: install the docker compose plugin (`apt install docker-compose-plugin`).
- Passwords live in a local `secrets.env` (gitignored). Migrating to SSH keys removes both the file and the expect scripts.
