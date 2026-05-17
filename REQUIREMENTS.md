# REQUIREMENTS

实验室计算资源计费 / 使用量统计系统的需求与设计记录。本文档先于实现存在，留作"未来扩展"的契约。

当前实现范围：**仅 HPC 每日核时统计入库**。NPU、LLM、登录等模块只做设计、不做实现。

---

## 1. 业务目标

为嘉庚创新实验室的共享计算资源（HPC、NPU、自建 LLM 推理服务）提供：

1. **使用量统计**：按用户、按组、按周/月维度查询。
2. **计费支撑**：按组（个人为单人组）累计消耗，输出账单。
3. **可视化**：现有 `http://10.26.15.53:18788` 仪表板上扩展更多视图。
4. **审批闭环**：API key 申请 → 管理员审批 → 自动开通账号 → 后续可追溯。

## 2. 资源系统覆盖

| 系统 | 当前状态 | 计费指标 | 数据源 |
|------|---------|---------|--------|
| **HPC** (10.26.15.51) | ✅ 已实现入库 | `core_hours` (CPU 核时), `job_count` | `sacct` (Slurm) |
| **NPU** (10.26.15.52) | ⏳ 规划中 | `card_hours` (Ascend 卡时) | 待调研：MindCluster / OpenStack quota / 自建 daemon |
| **LLM 推理** | ⏳ 规划中 | `tokens_in`, `tokens_out`, `requests`, `latency_p95` | vLLM/SGLang metrics / 自建网关 access log |

## 3. 计费单位

- **组（group）**：基本计费单位。
- **单人组**：单独使用某个资源的人按"个人组"开账单（`is_individual=1`），便于将来合并到正式课题组。
- 一个**人（person）**只属于一个组。
- 一个**人**可以拥有多个**账号（accounts）**：HPC 上叫 `lz`，NPU 上叫 `liangzhu`，LLM API key id 是个 UUID——这些都映射到同一个 person，进而归到同一个 group 计费。

## 4. 数据库 Schema

SQLite 单文件，路径 `/home/liangzhu/huawei-db/usage.sqlite`。

### 4.1 当前实现的表

```sql
groups (id, name UNIQUE, is_individual, billing_contact_email, created_at, notes)
person (id, name, email UNIQUE, role['user'|'admin'], group_id FK, created_at, notes)
accounts (id, person_id FK, system['hpc'|'npu'|'llm'], account_name, created_at,
          UNIQUE(system, account_name))
hpc_daily (date, account_name, core_hours, job_count, source, updated_at,
           PRIMARY KEY(date, account_name))
```

### 4.2 未来扩展（DDL 已在 `db/schema.sql` 注释中预留）

```sql
npu_daily (date, account_name, card_hours, job_count, source, updated_at, ...)
llm_daily (date, account_name, model, tokens_in, tokens_out, request_count,
           p95_latency_ms, source, updated_at, ...)

-- 登录系统（见 §6）
auth_user (id, person_id FK, username UNIQUE, password_hash, must_change_password,
           last_login_at, failed_attempts, locked_until, created_at)
auth_session (token, person_id FK, created_at, expires_at, ip, user_agent)
audit_log (id, ts, person_id FK, action, target, detail_json, ip)
```

### 4.3 设计决策

- **per-system 分表**（不是单一 `daily_usage`）：每个系统的指标列不同，分表后类型清晰、索引可单独优化、JOIN 简单。
- **`*_daily.account_name` 是字符串、不是外键**：新用户出现在 sacct 时直接落库，不会因 `person/accounts` 未补全而插入失败；管理员事后补映射，查询用 JOIN。
- **`groups + person` 分离**：未来个人转入正式组时只 UPDATE `person.group_id` 一行，历史数据不动。
- **`accounts.system` 用 CHECK 约束而非 ENUM**：SQLite 无 ENUM；CHECK 等价。

## 5. 数据流

```
hpc (10.26.15.51 / 172.16.12.2)
    │ ssh + sacct
    ▼
huawei-reports-web 容器 /opt/update/update-slurm.sh
    │
    ├─→ /usr/share/nginx/html/data/slurm_daily.json   (最近 30 天，用于图表)
    │
    └─→ /opt/db/usage.sqlite::hpc_daily               (历史全保留，用于计费/查询)

调度：crond 每天 03:00 (Asia/Shanghai)
首次部署：backfill_hpc.py 一次性拉 180 天 sacct，仅写 DB（不动 JSON）
```

未来 NPU、LLM 各自独立采集器，写各自的 `npu_daily` / `llm_daily` 表。

## 6. 登录系统（仅设计、未实现）

### 6.1 三方案对比

| 维度 | A. 厦大 CAS 统一身份 | B. 本地账户（推荐 v1） | C. Magic Link (邮件) |
|------|---------------------|----------------------|---------------------|
| UX | 单点登录，最佳 | 要记密码，普通 | 不用记密码，较好 |
| 部署难度 | 接入 IT 1-2 周 | 当天 | 配 SMTP 半天 |
| 校外用户 | 不行，要 fallback | 支持 | 支持 |
| 维护成本 | 低（IT 管） | 中（密码重置） | 中（SMTP 稳定性） |
| 推荐场景 | 用户>50 且都校内 | **起步** | 用户不想记密码 |

### 6.2 v1 推荐：本地账户

**流程**：
1. 用户提交 `apply.html` → `apikey-requests.jsonl`
2. 管理员审批 → 触发：
   - 在 `person` 表插入记录（默认 group = 个人单人组）
   - 在 `auth_user` 表插入记录（用户名 = 邮箱前缀，临时密码邮件发送）
   - 在 `accounts` 表给 LLM 系统建一条记录（API key）
3. 用户首次登录强制改密
4. Admin 用户可以：查所有人使用量、改 group 归属、审批新申请、查 audit_log

**安全要点**：
- bcrypt cost ≥ 12
- Session token 32 字节随机，HttpOnly + Secure cookie
- 速率限制：5 次登录失败锁定 15 分钟（`failed_attempts` + `locked_until`）
- HTTPS 必备（否则 password 明文过网，校园内网也不行）—— 当前 18788 是裸 HTTP，部署前需要先上 Let's Encrypt 或自签证书
- audit_log 记录所有 admin 操作

### 6.3 未来升级 A

如果用户量上 50+ 且 95% 校内人，再接入 CAS：保留 `person` 表，新增 `auth_external_id (person_id, provider, external_id)`，本地账户继续作为校外 fallback。

## 7. 访问控制（与登录系统同步实现）

| 角色 | 权限 |
|------|------|
| **anonymous** | 只能看 `index.html`（聚合图表，无个人数据）+ 提交 `apply.html` |
| **user** | 看自己的明细使用量、修改自己的联系信息 |
| **admin** | 看所有人 + 改组归属 + 审批申请 + 查 audit_log |

## 8. 当前 (2026-05-17) 实现清单

- [x] HPC schema 落地
- [x] 11 个 sacct 用户自动 seed 为 person + 单人组 + accounts(hpc)
- [x] backfill 180 天 sacct → hpc_daily
- [x] 每日 cron 增量 upsert 最近 31 天 → hpc_daily
- [x] 现有 chart 保持工作（仍读 slurm_daily.json）
- [ ] NPU 采集（待 NPU 资源管理方案确定）
- [ ] LLM 采集（待网关上线）
- [ ] 登录系统实现
- [ ] HTTPS
- [ ] 计费报表导出

## 9. 风险与待澄清

- **NPU 计费指标定义**：MindCluster 是否提供按用户细分的卡时？还是只能在 OpenStack 层面按 quota 估算？需要先调研。
- **LLM 计费**：vLLM 自带 metrics 是聚合的，按 API key 细分要么在网关层埋点，要么在 vLLM 前面架 LiteLLM-Proxy。
- **HTTPS**：内网证书签发——是用 step-ca 自建还是申请 letsencrypt（需要公网 DNS）。在登录系统上线前必须解决。
- **密码重置邮件**：要选 SMTP（校内邮箱 / 阿里云邮件推送 / 第三方）。
