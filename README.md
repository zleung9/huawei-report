# 华为服务器资源监控平台

嘉庚创新实验室共享计算资源（HPC / NPU / LLM）的使用量监控、计费统计与 API Key 申请平台。

> 校内访问地址：http://10.26.15.53:18788

## 系统架构

单容器部署，基于 nginx:alpine，包含三个核心进程：

- **nginx** — 监听 18788 端口，提供静态页面与 API 反向代理
- **crond** — 每日 03:00（Asia/Shanghai）自动采集 HPC 用量数据
- **apikey_server.py** — Python stdlib HTTP 服务，处理 API Key 申请（127.0.0.1:28789）
- **auth_server.py** — 登录认证服务（127.0.0.1:28790）

数据通过 crond 自动采集 HPC sacct 信息，写入 JSON（图表）与 SQLite（计费）。

## 仓库结构

```
huawei-report/
├── README.md                    # 本文件
├── REQUIREMENTS.md              # 需求与设计文档
├── secrets.env.example          # 密码模板（HPC + claw）
├── .gitignore
│
├── web/                         # Web 容器端（一次性部署）
│   ├── Dockerfile               # 标准镜像（HTML 需 bind-mount）
│   ├── Dockerfile.migrate       # 自包含镜像（HTML 内置，可 docker save 导出）
│   ├── nginx.conf               # nginx 配置，监听 18788
│   ├── crontab                  # 定时任务：每日 03:00 采集
│   ├── index.html               # 首页：总览图表（Chart.js）
│   ├── detail.html              # 管理员仪表盘：用户用量详情页
│   ├── apply.html               # API Key 申请页
│   ├── login.html               # 登录页（角色路由）
│   ├── register.html            # 注册页（含 HPC/NPU/LLM 账号申请）
│   ├── profile.html             # 用户个人页：账号信息 + 凭证管理
│   └── scripts/
│       ├── entrypoint.sh        # 容器入口：crond + backfill + nginx
│       ├── update-slurm.sh       # 每日 sacct 采集 + JSON/DB 更新
│       ├── collect_slurm.py      # sacct 解析：按用户/按天核时计算
│       ├── apikey_server.py      # API Key 申请处理服务
│       └── auth_server.py        # 登录认证服务
│
├── db/                          # 数据库工具（打包进镜像）
│   ├── schema.sql               # SQLite schema v3（含 auth + credential）
│   ├── init_db.py               # 数据库初始化
│   ├── backfill_hpc.sh          # 首次部署：180 天历史回填
│   ├── aggregate_hpc.py         # HPC 数据聚合
│   ├── admin_bootstrap.py       # 管理员账号初始化
│   └── migrate_v3.py            # v2→v3 迁移：accounts 增加 credential 列
│
├── skill/                       # Claude Code 技能（手动备用）
│   ├── SKILL.md                 # 技能声明
│   ├── run.sh                   # 编排脚本
│   ├── collect_slurm.py         # sacct 解析
│   ├── pull-sacct.exp           # expect 脚本：ssh hpc 采集
│   └── push-json.exp            # expect 脚本：scp 推送 JSON
│
└── deploy/                      # 部署辅助
    ├── remote-setup.sh          # 服务器端容器启动脚本
    ├── export-image.sh          # 构建自包含镜像并导出 tar.gz
    ├── start-container.sh       # 从导出镜像启动容器
    └── install-skill.sh         # 本地安装 Claude Code 技能
```

## 数据库设计

SQLite 单文件，路径 `/home/liangzhu/huawei-db/usage.sqlite`。

### 已实现的表

| 表名 | 用途 |
|------|------|
| `groups` | 计费组（单人组 `is_individual=1`）|
| `person` | 自然人，关联一个组 |
| `accounts` | 人员 × 系统(HPC/NPU/LLM) × 账号名 + 凭证 |
| `hpc_daily` | HPC 每日核时（date + account_name 联合主键）|
| `auth_user` | 登录账号（PBKDF2-SHA256）|
| `auth_session` | 登录会话 |
| `audit_log` | 操作审计日志 |

### 设计要点

- **per-system 分表**：HPC/NPU/LLM 指标不同，各自独立表，类型清晰、索引可单独优化
- **hpc_daily.account_name 不是外键**：新用户出现在 sacct 时直接落库，管理员事后补映射
- **groups + person 分离**：个人转入正式组时只改 `person.group_id`，历史数据不动

## 功能页面

| 页面 | 功能 | 权限 |
|------|------|------|
| `index.html` | 总览页：4 项统计指标 + 按用户堆叠柱状图 + 排行榜 | 公开 |
| `detail.html` | 管理员仪表盘：按用户堆叠柱状图 + 排行榜 | 仅 admin |
| `apply.html` | API Key 申请表单 | 公开 |
| `login.html` | 用户登录页（admin→仪表盘，user→个人页） | 公开 |
| `register.html` | 注册页（可选申请 HPC/NPU/LLM 账号） | 公开 |
| `profile.html` | 个人页：账号信息 + HPC/NPU 密码 + API Key（显示/隐藏/复制） | 登录用户 |

### 角色说明

- **admin** — 查看所有用户 HPC 用量（detail.html 仪表盘），管理后台
- **user** — 查看个人页面（profile.html），查看自己的 HPC/NPU 账号密码和 API Key

## 用户注册与账号分配

1. 用户访问 `register.html` 填写注册信息（姓名、邮箱、用户名、密码）
2. 可选申请 HPC/NPU 账号（指定账号名）和 LLM API Key
3. 注册成功后：
   - HPC/NPU：自动生成随机密码，存储在 `accounts.credential`
   - LLM：自动生成 API Key（`sk-{48hex}`），存储在 `accounts.credential`
4. 用户登录后可在 `profile.html` 查看账号密码和 API Key（支持显示/隐藏、一键复制）

## 旧版 API Key 申请流程

> 以下为 apply.html 的旧流程，新用户建议使用 register.html 一站式注册。

1. 用户访问 `apply.html` 填写申请表（姓名、用途、联系方式等）
2. 提交 POST 请求至 `/api/apikey-request`，数据追加到 `apikey-requests.jsonl`
3. nginx 对 JSONL 文件返回 404，防止直接下载
4. 管理员通过 `ssh claw` 查看申请，审批后开通账号

## 部署

### 前提条件

- Docker 已安装在华为服务器（10.26.15.53）
- 可 SSH 访问 HPC（10.26.15.51 / 内网 172.16.12.2）
- 容器使用 `--network host` 模式（Docker 默认 bridge 无法路由 172.16.12.x）

### 方式一：自包含镜像（推荐）

使用 `Dockerfile.migrate` 构建自包含镜像，将 HTML 页面和 nginx.conf 内置到镜像中。部署到新服务器时无需额外文件，只需一个 SQLite 挂载卷。

```bash
# 1. 构建 + 导出
cd ~/workspace/huawei-report
bash deploy/export-image.sh
# 产出: huawei-reports-web.tar.gz (~40MB)

# 2. 传输到服务器
scp huawei-reports-web.tar.gz liangzhu@10.26.15.53:/home/liangzhu/

# 3. 在服务器上加载 + 启动
ssh liangzhu@10.26.15.53
docker load < huawei-reports-web.tar.gz
HPC_PASSWORD=xxx ADMIN_EMAIL=admin@example.com ADMIN_PASSWORD=Huawei2024Admin \
  bash deploy/start-container.sh
```

`start-container.sh` 会在首次启动时：
- 自动运行数据库初始化或迁移（v2→v3）
- 创建管理员账号（`ADMIN_EMAIL` / `ADMIN_PASSWORD` 环境变量）
- 回填 180 天 HPC 历史数据（仅首次）
- 启动 crond、auth_server、apikey_server、nginx

> 镜像更新后重复步骤 1–3 即可。容器入口不会覆盖已有的数据文件（JSON、数据库）。

### GitHub Release 发布

导出的镜像通过 GitHub Release 管理（不提交到 git 仓库），方便版本回溯：

```bash
# 构建并上传到 GitHub Release
bash deploy/release-image.sh [VERSION_TAG]
# 默认 tag: latest；也可指定版本号如 v3

# 在目标服务器下载
gh release download latest --repo zleung9/huawei-report
```

### 方式二：标准镜像 + bind-mount

使用标准 `Dockerfile`，HTML 页面和 nginx.conf 通过 bind-mount 从宿主机加载：

```bash
cd ~/workspace/huawei-report
# 打包部署文件
STAGE=$(mktemp -d)
cp -R web/Dockerfile web/crontab web/nginx.conf web/index.html web/apply.html web/scripts "$STAGE/"
cp -R db "$STAGE/db"
tar czf /tmp/deploy-bundle.tgz -C "$STAGE" .
rm -rf "$STAGE"

# 推送到服务器
set -a; source secrets.env; set +a
expect /tmp/deploy-to-claw.exp /tmp/deploy-bundle.tgz deploy/remote-setup.sh
```

### 容器挂载

| 宿主机路径 | 容器路径 | 用途 |
|-----------|---------|------|
| `/home/liangzhu/huawei-db/` | `/opt/db-data` | SQLite 数据库（rw）|

> 使用自包含镜像时，HTML 和 nginx.conf 已内置，无需额外 bind-mount。

### 管理员账号

容器启动时自动创建管理员账号，通过环境变量配置：

| 变量 | 说明 | 默认值 |
|------|------|-------|
| `ADMIN_EMAIL` | 管理员邮箱 | `admin@example.com` |
| `ADMIN_PASSWORD` | 管理员密码 | `Huawei2024Admin` |

> 生产环境请务必修改默认密码。

### 数据库迁移

数据库 schema 使用版本管理（`schema_version` 表）：

| 版本 | 变更 |
|------|------|
| v1 | 基础表：groups, person, accounts, hpc_daily |
| v2 | 认证表：auth_user, auth_session, audit_log |
| v3 | accounts 增加 `credential` 列（HPC/NPU 密码、LLM API Key） |

容器入口会自动检测当前版本并运行迁移脚本 `migrate_v3.py`，无需手动操作。

## 网络注意事项

- **claw -> hpc TCP:22 经 10.26.15.51 被防火墙拦截**：Ping 通（NAT），但 SSH 超时。使用内网 IP `172.16.12.2`（hpc 和 claw 均在 172.16.12.0/24）
- **容器需要 `--network host`**：Docker 默认 bridge 无法路由到 172.16.12.x 网络。因此 nginx 直接监听 18788 端口（无端口映射）

## 数据流

```
HPC (172.16.12.2)
    | ssh + sacct
    v
huawei-reports-web 容器 /opt/update/update-slurm.sh
    |
    +-> /usr/share/nginx/html/data/slurm_daily.json  (30天图表)
    |
    +-> /opt/db-data/usage.sqlite::hpc_daily         (历史全保留计费)

调度：crond 每天 03:00 (Asia/Shanghai)
首次部署：backfill_hpc.sh 一次性拉 180 天 sacct
```

## Claude Code 技能（手动备用）

容器内的 crond 已接管自动更新。本地 Mac 侧的 Claude Code 技能（`~/.claude/skills/huawei-report-update/`）作为手动备用：

```bash
# 通过 Claude Code
> 更新华为周报

# 或直接运行
bash ~/workspace/huawei-report/skill/run.sh
DAYS=60 bash ~/workspace/huawei-report/skill/run.sh   # 自定义时间窗口
```

## 开发路线

- [x] HPC 每日核时统计图表
- [x] SQLite 使用量数据库 + HPC 历史回填
- [x] API Key 申请表单
- [x] 登录认证系统（PBKDF2-SHA256 + session token）
- [x] 每日自动采集（容器内 crond）
- [x] 用户注册 + HPC/NPU/LLM 账号自动分配
- [x] 角色路由（admin→仪表盘，user→个人页）
- [x] 个人页凭证管理（密码/API Key 显示/隐藏/复制）
- [x] 自包含 Docker 镜像（Dockerfile.migrate）
- [x] Schema v3 迁移（accounts.credential）
- [ ] NPU 用量采集（待 NPU 资源管理方案确定）
- [ ] LLM 用量采集（待网关上线）
- [ ] HTTPS 部署
- [ ] 计费报表导出

## 已知问题

- 服务器上的 `docker-compose v2.4.1` 与 Docker 29.2.1 API 不兼容，使用 `docker run` 代替
- 密码存储在本地 `secrets.env`（已 gitignore），未来迁移至 SSH 密钥认证
- 当前为 HTTP（端口 18788），登录系统上线前需要先部署 HTTPS

## 相关文档

- [REQUIREMENTS.md](REQUIREMENTS.md) — 完整需求与设计文档（含登录系统方案对比、计费模型等）
