# 设计决策记录（ADR）

每份记录写一个重大决定：为什么选 A 不选 B。具体怎么做写在 [设计文档](../design/README.md)。何时必须写、写作规矩见 [开发规范 §7](../开发规范.md#7-设计文档与-adr)。

- 动手前先看这里；要推翻已接受的决定，先新写一份 ADR 说明理由，经用户确认后把旧的标为“已被 NNNN 取代”。
- 文件名：`NNNN-简述.md`，编号递增，不复用。
- 状态：草稿 / 已接受 / 已被 NNNN 取代。
- 已接受的 ADR 基本不改；0001–0004 写于模板更新前，缺“评估标准”“何时重新评估”的，下次修改时补上。

## 索引

| 编号 | 决定 | 状态 |
| --- | --- | --- |
| 早期 | [多源检索：Tavily + Exa 并发，DuckDuckGo 兜底](../RETRIEVAL_SOURCES_DECISION.md) | 已接受 |
| [0001](0001-python-fastapi.md) | 平台后端用 Python + FastAPI | 已接受 |
| [0002](0002-postgres-task-queue.md) | PostgreSQL 存业务数据，任务表兼作队列，不引入 Kafka | 已接受 |
| [0003](0003-free-quota-and-own-keys.md) | 内测：免费额度限一个模型，另支持用户自带 key | 已被 0007 取代 |
| [0004](0004-known-providers-only.md) | 用户自带 key 只能从已知服务商中选择 | 已被 0007 取代 |
| [0005](0005-login-server-session-cookie.md) | 登录用服务端会话 + HttpOnly Cookie，不用 JWT | 已接受 |
| [0006](0006-sqlalchemy-alembic-sync.md) | 数据库访问用 SQLAlchemy（同步）+ Alembic，离线测试默认用 SQLite | 已接受 |
| [0007](0007-public-trial-github-login.md) | 在线版改为公开试用：GitHub 登录、按次数限额，不做自带 key | 已接受 |
| [0008](0008-admin-aggregate-dashboard.md) | 管理员运营面板只提供汇总统计，不扩展私有研究读取权限 | 草稿 |

| [0009](0009-platform-roles-and-user-quotas.md) | 三级平台角色之外保留访客，支持个人免费次数管理 | 草稿 |

## 模板

```markdown
# NNNN：标题

- 状态：草稿
- 日期：YYYY-MM-DD
- 作者：用户 / Codex / Claude Code
- 关联：#issue、相关 ADR 或设计文档

## 背景

要解决什么问题；有哪些约束（内存、成本、人手、已有代码……）。

## 评估标准

先定拿什么比，再比方案。例如：可靠性、运维成本、改动量、学习价值。

## 候选方案

至少两个真实考虑过的方案，“维持现状”也算一个。

| 标准 | 方案 A | 方案 B | 维持现状 |
| --- | --- | --- | --- |

## 决定

## 代价

选了它要付出什么，照实写。

## 何时重新评估

出现什么情况要回来重新考虑，例如“日任务量超过 1 万”“需要跨机器部署”。
```
