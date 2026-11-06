---
name: finish-task
description: 本仓库任务完成后收尾：核对范围、按改动选测试、提交、用 yy2511 身份推送功能分支、按模板开 PR 并等待 Basic checks。在 research-workbench-clean 里实现完一个 issue、准备开 PR 时使用。不合并 PR。
---

# 完成任务

流程背景见 `docs/开发流程.md`。每一步做完再进入下一步；任何一步失败，先修好或如实写进 PR，不跳过。

## 1. 核对范围

```bash
git fetch origin
git diff --stat origin/main...HEAD
git status --short
```

- 改动应都在 issue 的“要做什么”之内。范围外的改动拆出去，记成新 issue。
- `git status` 里不应出现 `.env`、`runs/`、`plans/`、`runtime_config.json`、`provider_connections.json`、`.claude/settings.local.json`。
- 带 `需要设计` 标签的 issue：实现和设计文档不一致的地方，在本 PR 更新设计文档的正文和“实施记录”。

## 2. 按改动选测试

只跑离线测试。下表按改动路径累加：

| 改了 | 运行 |
| --- | --- |
| 任意 `.py` | `uvx ruff@0.17.3 check --select E9,F63,F7,F82 .`（与 CI 相同） |
| `src/`、`tests/`、`scripts/`、`eval/`、`pyproject.toml`、`uv.lock` | `uv run pytest` |
| `web/` | `npm --prefix web test`、`npm --prefix web run lint`、`npm --prefix web run build` |
| `deploy/`、`Dockerfile`、`compose*.yaml`、`sandbox_image/` | `uv run pytest -q tests/test_container_release.py` |
| 只有文档 | 检查文中相对链接指向的文件存在 |

- `--run-live`、`--run-docker`、`--run-browser` 需用户明确同意；没跑就写进 PR 的“没有验证的部分”，并注明 CI 会跑哪些。
- 记下实际命令和结果（通过数 / 失败数），PR 里照抄，不要概括成“测试通过”。

## 3. 提交

- 中文一句话写清结果，末尾带 issue 号：`拆出研究相关接口到 routers/research.py，行为不变 (#8)`。
- 按所用工具的署名要求追加署名行。
- 只 `git add` 本任务的文件，不用 `git add -A`。

## 4. 推送

本机 gh 同时登录 `yy2511`（仓库所有者）和 `emberecho2`（只读），当前账号可能是任意一个。推送和 gh 命令都临时指定 `yy2511`，不切换全局账号，不打印令牌：

```bash
export GH_TOKEN=$(gh auth token --user yy2511)
git push -u origin HEAD
```

云端环境没有这两个账号时，使用环境自带的 GitHub 授权。不推送 `main`。

## 5. 开 PR

```bash
gh issue view <N> -R yy2511/deep-research-agent --json title,labels,milestone
```

用 `gh pr create -R yy2511/deep-research-agent --base main` 开 PR：

- 标题：与 issue 一致或更具体。
- 标签：沿用 issue 的类型、模块标签，加上 `agent:codex` 或 `agent:claude`。
- 里程碑：issue 有就带上（`--milestone`）。
- 正文按 `.github/pull_request_template.md` 的各节填写，不删节，不适用写“无”：
  - `Closes #N`
  - 改了什么：大白话；改了界面附截图
  - 实际跑过的验证：第 2 步的命令和结果
  - 没有验证的部分
  - 设计文档与 ADR
  - 影响的限制：本次新增、移除，或沿用但影响该流程的限制
  - 作者

## 6. 等检查

```bash
gh pr checks <PR号> -R yy2511/deep-research-agent --watch
```

Basic checks 失败就修复后重新推送。改了代码时，另看 “CI and container image” 工作流的结果，写进 PR 评论。

## 7. 汇报

告诉用户：PR 链接、实际验证、没验证的部分、限制变化。**不合并**，合并由用户决定。
