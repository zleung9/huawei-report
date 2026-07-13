# HPC 核时采集改用 SSH Key 的修改说明与部署指南

## 背景

华为周报站点的 HPC 核时统计由容器内定时任务每天 03:00 自动更新。

原链路使用 `sshpass + HPC_PASSWORD` 登录 HPC 节点：

```text
liangzhu_huawei-admin 容器
  -> /opt/update/update-slurm.sh
  -> sshpass 使用 HPC_PASSWORD 登录 lz@172.16.12.2
  -> sacct 拉取作业记录
  -> 生成 /usr/share/nginx/html/data/slurm_daily.json
```

这次线上日志显示：

```text
[2026-07-07T03:00:00+0800] pulling sacct from lz@172.16.12.2 (last 30d)
Permission denied, please try again.
```

说明 `crond` 正常触发，网页也正常读取 JSON，失败点在容器通过密码登录 HPC 时认证失败。

因此本次改造目标是：**改为 SSH key 登录 HPC，减少对 `HPC_PASSWORD` 的依赖**。

## 修改逻辑

采集脚本现在支持两种认证方式：

```text
优先级 1：如果设置了 HPC_SSH_KEY，则使用 SSH key 登录 HPC
优先级 2：如果没有设置 HPC_SSH_KEY，则回退到原来的 HPC_PASSWORD
两者都没有：脚本报错退出
```

也就是说，旧的密码部署方式仍然兼容，但新部署推荐使用：

```text
HPC_SSH_KEY=/opt/ssh/id_ed25519
```

并将宿主机上的私钥以只读方式挂载进容器：

```text
/home/liangzhu/huawei-ssh/hpc_lz_ed25519:/opt/ssh/id_ed25519:ro
```

## 代码改动汇总

### `web/scripts/update-slurm.sh`

每日核时采集脚本。

改动：

- 支持 `HPC_SSH_KEY`
- `HPC_SSH_KEY` 存在时使用：

```bash
ssh -i "$HPC_SSH_KEY" ...
```

- 未设置 `HPC_SSH_KEY` 时才使用旧的：

```bash
sshpass -p "$HPC_PASSWORD" ssh ...
```

- 增加 SSH 超时参数：

```text
ConnectTimeout=20
ServerAliveInterval=15
ServerAliveCountMax=2
NumberOfPasswordPrompts=1
```

### `db/backfill_hpc.sh`

历史核时回填脚本。

改动与 `update-slurm.sh` 一致，支持 `HPC_SSH_KEY`，并保留 `HPC_PASSWORD` 回退。

### `deploy/start-container.sh`

正式启动容器脚本。

改动：

- 默认查找宿主机 key：

```text
/home/liangzhu/huawei-ssh/hpc_lz_ed25519
```

- 如果 key 文件存在，自动给容器增加：

```bash
-e HPC_SSH_KEY=/opt/ssh/id_ed25519
-v /home/liangzhu/huawei-ssh/hpc_lz_ed25519:/opt/ssh/id_ed25519:ro
```

- 如果 key 文件不存在，则继续要求 `HPC_PASSWORD`。

### `deploy/remote-setup.sh`

远程部署脚本。

改动与 `deploy/start-container.sh` 一致。

### `web/docker-compose.yml`

Docker Compose 部署配置。

改动：

- 支持通过 `.env` 设置：

```text
HPC_SSH_KEY=/opt/ssh/id_ed25519
HPC_SSH_KEY_SRC=/home/liangzhu/huawei-ssh/hpc_lz_ed25519
```

- 同时保留：

```text
HPC_PASSWORD=
```

作为回退。

### `web/.env.example`

示例配置改为 SSH key 优先。

### `deploy/export-image.sh`

导出镜像后的提示命令更新为 SSH key 方式，避免继续提示使用 `HPC_PASSWORD`。

## 管理员需要做什么

### 1. 在部署服务器生成 SSH key

在 `10.26.15.53` 上执行：

```bash
sudo mkdir -p /home/liangzhu/huawei-ssh

sudo ssh-keygen -t ed25519 \
  -f /home/liangzhu/huawei-ssh/hpc_lz_ed25519 \
  -C "huawei-report sacct collector" \
  -N ""

sudo chmod 700 /home/liangzhu/huawei-ssh
sudo chmod 600 /home/liangzhu/huawei-ssh/hpc_lz_ed25519
sudo chmod 644 /home/liangzhu/huawei-ssh/hpc_lz_ed25519.pub
```

生成两个文件：

```text
/home/liangzhu/huawei-ssh/hpc_lz_ed25519      私钥，不要外传
/home/liangzhu/huawei-ssh/hpc_lz_ed25519.pub  公钥，可加入 HPC authorized_keys
```

### 2. 将公钥加入 HPC

查看公钥：

```bash
sudo cat /home/liangzhu/huawei-ssh/hpc_lz_ed25519.pub
```

将输出的整行加入 HPC 节点 `172.16.12.2` 上 `lz` 用户的：

```text
~/.ssh/authorized_keys
```

建议在 `authorized_keys` 中限制该 key 的能力，例如：

```text
no-agent-forwarding,no-X11-forwarding,no-port-forwarding,no-pty ssh-ed25519 ...
```

### 3. 在部署服务器测试 key

在 `10.26.15.53` 上执行：

```bash
sudo ssh -i /home/liangzhu/huawei-ssh/hpc_lz_ed25519 \
  -o BatchMode=yes \
  -o StrictHostKeyChecking=no \
  lz@172.16.12.2 \
  'hostname'
```

继续测试 `sacct`：

```bash
sudo ssh -i /home/liangzhu/huawei-ssh/hpc_lz_ed25519 \
  -o BatchMode=yes \
  -o StrictHostKeyChecking=no \
  lz@172.16.12.2 \
  'sacct -aX --starttime=now-1days --format=User,JobID,State,AllocCPUS,Elapsed -P | head'
```

这一步必须成功后再部署容器。

### 4. 拉取新代码并构建镜像

在部署服务器上拉取包含本次修改的代码：

```bash
cd /home/liangzhu/huawei-report
git fetch origin
git pull
```

构建并导出镜像：

```bash
bash deploy/export-image.sh
```

或者直接构建本地镜像：

```bash
docker build -f web/Dockerfile.migrate -t huawei-reports-web:latest web
```

### 5. 重建正式容器

如果使用本仓库的启动脚本，并且 key 已放在默认位置：

```bash
/home/liangzhu/huawei-ssh/hpc_lz_ed25519
```

则脚本会自动挂载 key：

```bash
ADMIN_EMAIL=... ADMIN_PASSWORD=... LLM_API_AUTH=... \
  bash deploy/start-container.sh
```

如果手动 `docker run`，需要确保包含：

```bash
-e HPC_SSH_KEY=/opt/ssh/id_ed25519
-v /home/liangzhu/huawei-ssh/hpc_lz_ed25519:/opt/ssh/id_ed25519:ro
```

完整示例：

```bash
docker run -d \
  --name liangzhu_huawei-admin \
  --restart unless-stopped \
  --network host \
  -e HPC_HOST=172.16.12.2 \
  -e HPC_USER=lz \
  -e HPC_SSH_KEY=/opt/ssh/id_ed25519 \
  -e DAYS=30 \
  -e ADMIN_EMAIL=... \
  -e ADMIN_PASSWORD=... \
  -e LLM_API_AUTH=... \
  -v /home/liangzhu/huawei-db:/opt/db-data \
  -v /home/liangzhu/huawei-reports/data:/usr/share/nginx/html/data \
  -v /home/liangzhu/huawei-ssh/hpc_lz_ed25519:/opt/ssh/id_ed25519:ro \
  huawei-reports-web:latest
```

如果正式容器名不是 `liangzhu_huawei-admin`，请按实际容器名调整。

## 部署后验证

### 1. 检查容器状态

```bash
docker ps --filter name=liangzhu_huawei-admin
docker logs --tail 100 liangzhu_huawei-admin
```

### 2. 手动触发核时更新

```bash
docker exec liangzhu_huawei-admin sh -lc '/opt/update/update-slurm.sh'
```

成功时应看到类似：

```text
[... ] pulling sacct from lz@172.16.12.2 (last 30d)
[... ] auth: using SSH key /opt/ssh/id_ed25519
[... ] sacct: ... lines
wrote /usr/share/nginx/html/data/slurm_daily.json (...)
[... ] wrote /usr/share/nginx/html/data/slurm_daily.json
[... ] hpc_daily upsert ok
```

### 3. 检查 JSON 文件更新时间

```bash
docker exec liangzhu_huawei-admin sh -lc '
ls -l /usr/share/nginx/html/data/slurm_daily.json
tail -50 /var/log/update/update.log
'
```

### 4. 检查 HTTP 侧更新时间

```bash
curl -I http://10.26.15.53:18788/data/slurm_daily.json
```

`Last-Modified` 应接近当前时间。

### 5. 检查网页

打开：

```text
http://10.26.15.53:18788
```

确认“数据更新”时间已变新。

## 回滚办法

部署前建议保留旧容器：

```bash
docker stop liangzhu_huawei-admin
docker rename liangzhu_huawei-admin liangzhu_huawei-admin.bak-$(date +%Y%m%d%H%M)
```

如果新容器失败，可停止并删除新容器，再恢复旧容器：

```bash
docker stop liangzhu_huawei-admin
docker rm liangzhu_huawei-admin

docker rename <旧备份容器名> liangzhu_huawei-admin
docker start liangzhu_huawei-admin
```

## 安全注意事项

- 不要把私钥 `/home/liangzhu/huawei-ssh/hpc_lz_ed25519` 发到聊天、邮件或 GitHub。
- 不要提交 `.env`、`secrets.env`、私钥文件。
- 不要粘贴完整 `docker inspect` 输出，其中可能包含环境变量和密码。
- `HPC_PASSWORD` 仍可回退使用，但不推荐继续作为长期方案。
- 后续建议把 LLM 采集日志中的认证头脱敏，避免 `Authorization` 信息出现在日志里。

## 一句话说明

本次修改后，正式容器只要挂载：

```text
/home/liangzhu/huawei-ssh/hpc_lz_ed25519:/opt/ssh/id_ed25519:ro
```

并设置：

```text
HPC_SSH_KEY=/opt/ssh/id_ed25519
```

就会自动使用 SSH key 登录 `lz@172.16.12.2` 拉取 `sacct`，不再依赖失效的 `HPC_PASSWORD`。
