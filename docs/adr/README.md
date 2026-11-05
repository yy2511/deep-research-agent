# 设计决策记录（ADR）

每份记录写一个重大决定：定了什么、为什么、考虑过哪些别的选择、带来什么代价。

- 动手前先看这里；要推翻已接受的决定，先新写一份 ADR 说明理由，经用户确认后把旧的标为“已被 NNNN 取代”。
- 文件名：`NNNN-简述.md`，编号递增，不复用。
- 状态：草稿 / 已接受 / 已被 NNNN 取代。

## 索引

| 编号 | 决定 | 状态 |
| --- | --- | --- |
| 早期 | [多源检索：Tavily + Exa 并发，DuckDuckGo 兜底](../RETRIEVAL_SOURCES_DECISION.md) | 已接受 |
| [0001](0001-python-fastapi.md) | 平台后端用 Python + FastAPI | 已接受 |
| [0002](0002-postgres-task-queue.md) | PostgreSQL 存业务数据，任务表兼作队列，不引入 Kafka | 已接受 |
| [0003](0003-free-quota-and-own-keys.md) | 内测：免费额度限一个模型，另支持用户自带 key | 已接受 |
| [0004](0004-known-providers-only.md) | 用户自带 key 只能从已知服务商中选择 | 已接受 |

## 模板

```markdown
# NNNN：标题

- 状态：草稿
- 日期：YYYY-MM-DD

## 背景
## 决定
## 考虑过的其他选择
## 代价与影响
```
