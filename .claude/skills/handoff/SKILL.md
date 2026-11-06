---
name: handoff
description: 本仓库任务没做完要停下时交接：把半成品提交推送到功能分支、确保有草稿 PR、在 PR 上发一条结构化“交接”评论，让另一个 AI、云端会话或用户能接着做。上下文快满、用户要结束、被问题卡住、换工具或换设备时使用；接手别人的任务时也按“接手”一节读取。
---

# 交接

进度以 GitHub 为准。交接后，任何会话只看 GitHub 就能接着做，不依赖本次聊天记录。

## 交出

### 1. 提交半成品

```bash
git status --short
git add <本任务的文件>
git commit -m "WIP：<做到哪一步> (#N)"
```

- 测试不必通过，但要在交接评论里写明现状。
- 不提交密钥、`.env`、运行记录和本机配置。
- 合并时会压成一个提交，WIP 提交不会留在 `main`。

### 2. 推送

```bash
export GH_TOKEN=$(gh auth token --user yy2511)
git push -u origin HEAD
```

（为什么指定 `yy2511`：见 `.claude/skills/finish-task/SKILL.md` 第 4 步。）

### 3. 确保有 PR

```bash
gh pr list -R yy2511/deep-research-agent --head "$(git branch --show-current)"
```

没有就开草稿 PR：`gh pr create --draft`，正文先写 `Closes #N` 和一句话目标，其余按模板留待完成时补。

### 4. 发交接评论

`gh pr comment <PR号> -R yy2511/deep-research-agent --body-file -`，正文：

```markdown
## 交接（YYYY-MM-DD，<Codex / Claude Code / 云端会话>）

**做到哪了**：一句话。

**已完成**
- 

**下一步**（具体到文件、函数）
1. 

**验证情况**：跑过的命令和结果；没跑的写明。

**坑与注意**：踩过的问题、看起来能做但不该做的事。

**需要用户定的**：没有写“无”。

**没有推送的东西**：只在本机的文件或数据；没有写“无”。
```

### 5. 告诉用户

PR 链接和一句话现状。

## 接手

```bash
export GH_TOKEN=$(gh auth token --user yy2511)
gh pr view <PR号> -R yy2511/deep-research-agent --comments
gh issue view <N> -R yy2511/deep-research-agent --comments
git fetch origin && git switch <分支>
```

1. 读 issue、PR 描述、最近一条“交接”评论，再看分支上的提交。
2. 交接里“需要用户定的”未解决时，先问用户。
3. 同一分支同一时间只有一个会话在改；开始前确认上一个会话已停下。
4. 继续时改 PR 的 `agent:` 标签为自己。
