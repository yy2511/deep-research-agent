# 部署与发布

> 本文只描述当前可执行的部署合同。当前版本、验证结果和未复核项以 [`../STATUS.md`](../STATUS.md) 为准。发布脚本与 CI 于 2026-10-26 从迁出基准 `feat/skills@85f400b` 恢复，并按现役镜像（含浏览器）调整。

## 当前拓扑

```text
GitHub Actions（yy2511/deep-research-agent，main 分支）
  → 后端测试 + 执行沙箱实测 + 前端 test/lint/build
  → 构建并 smoke test linux/amd64 镜像
  → 私有 GHCR：sha-<完整 Git SHA>@sha256:<digest>
  → 人工 stage → verify → activate
  → Docker Compose（127.0.0.1:8765）
  → Caddy HTTPS + Basic Auth
  → https://deepresearch.emberecho.de
```

Caddy 是唯一公网入口；应用容器只绑定 loopback。匿名请求必须在 Caddy 层返回 401，避免外部读取运行历史、修改模型配置或触发付费研究。

发布到 GHCR **不等于部署**。`main` 更新后生产不会自动拉取或重启，必须使用受控发布脚本切换。2026-08 的旧代码线保留在 `dag-v1` 分支，不再触发构建。

## 状态与密钥边界

生产根目录默认为 `/home/deploy/apps/deep-research-agent`：

```text
shared/
  .env                 应用密钥，0600
  runtime_config.json  Web 模型配置
  runs/                Web 运行记录
  plans/               Planner 尝试与诊断
container/
  compose.yaml
  container_release.py
  releases/            不可变镜像记录与 env
  active.json
  previous.json
  pending.json         仅在切换中存在
```

`shared/` 以 bind mount 挂到容器 `/data`。密钥只通过 Compose 的 `env_file` 注入，不进入镜像、release 记录或 Git。生产 Compose 将容器进程映射为宿主机数据所有者 1000:1000；镜像自身的默认非 root uid/gid 是 999。

旧 `dra-web.service` 只作为首次容器切换的已记录 previous 保留，平时必须 disabled/inactive。不要手工同时启用 systemd 与 Compose，否则主机重启后会争抢 8765。

## CI 与镜像

[`.github/workflows/container-image.yml`](../.github/workflows/container-image.yml) 在 pull request、`main` 的运行相关文件变化或人工触发时执行：

1. `uv sync --locked` 后运行 `uv run pytest`，再构建执行沙箱镜像并跑真实 Docker 执行测试；
2. `npm ci` 后运行 `npm test`、`npm run lint`、`npm run build`；
3. 非 PR 事件构建 `linux/amd64` 镜像并发布完整 SHA tag；
4. 从 GHCR 拉回镜像，检查架构、`/healthz`、首页、非 root 用户、生产依赖；浏览器所需的 Node、Playwright MCP 和 Chromium 必须存在，npm、uv、`.env` 和前端源码不应存在；
5. 全部通过后，`main` 分支的构建才把 `main` 便利 tag 指向同一 digest。

服务器版本必须使用完整 `sha-<40 位 Git SHA>` tag 和 digest，不能使用会移动的便利 tag。服务器只保存 GHCR 只读凭据；Actions 使用任务级 `GITHUB_TOKEN` 发布。

## 生产发布与回滚

发布前从成功的 Actions run 取得完整 tag 和 digest，然后在生产机执行：

```bash
cd /home/deploy/apps/deep-research-agent/container

./container_release.py stage \
  ghcr.io/yy2511/deep-research-agent:sha-<40位Git-SHA> \
  sha256:<64位镜像摘要>

./container_release.py verify <40位Git-SHA>
./container_release.py status
./container_release.py activate <40位Git-SHA>
```

各阶段含义：

- `stage`：校验不可变坐标、拉取镜像并记录 release；
- `verify`：用独立 Compose project 在 `127.0.0.1:18766` 挂载真实 shared 数据，检查首页和 `/healthz`，随后无条件删除 canary；
- `activate`：只接受已验证 release，记录 previous 后切换生产；失败时尝试恢复切换前状态；
- `status`：显示 systemd、Compose、active、previous 和 pending；
- `rollback`：只回到脚本记录的 previous，不猜测目录或 tag。

```bash
./container_release.py rollback
./container_release.py status
```

如果存在 `pending.json`，先查明上一次切换停在哪一步；不要手工删除状态文件或绕过脚本。发布、回滚和密钥轮换都不应实际发起研究，以免产生模型与检索费用。

## 本地容器验证

本地 `.env` 和持久目录必须先存在；`compose.yaml` 设置 `create_host_path: false`，路径拼错时会明确失败，避免 Docker 静默创建 root 所有的空目录。

```bash
cp .env.example .env
mkdir -p .docker-data

docker build --pull=false -t dra-web:local .
docker compose up -d
docker compose ps

curl --fail http://127.0.0.1:8765/healthz
curl --fail http://127.0.0.1:8765/
```

验证完成后可停止本地服务；不要删除 `.docker-data/`，除非已经确认其中没有需要保留的运行记录。

## 健康检查与验收

容器内 `GET /healthz` 只检查：

- Web 进程可响应；
- active run 计数可读取；
- `data/runs/plans` 三个持久目录可创建并清理探针文件。

它不读取模型配置，也不调用 LLM、搜索或研究编排。容器内部正常返回 200，持久目录不可写时返回 503；公网访问仍由 Caddy Basic Auth 保护，因此匿名访问 `/healthz` 返回 401 是预期行为。

一次生产切换至少核对：

1. `container_release.py status` 无 pending，active revision 与目标 SHA 一致；
2. Compose 容器为 `healthy` 且 restart count 未增加；
3. loopback 首页和 `/healthz` 返回 200；
4. 公网匿名访问返回 401，使用密码管理器中的凭据后首页返回 200；
5. `runtime_config.json`、历史 run 和 shared 文件在切换前后保持；
6. 其他同机服务不受影响。

## 当前未覆盖

- **线上运行代码**：应用跑在容器里，研究中的 Bash 需要宿主机 Docker 和执行沙箱镜像（`compose.execution.yaml`）。`container_release.py` 目前只使用单个 `compose.yaml`，不带这份叠加文件，所以按本流程发布的线上实例不能运行代码，网页“运行代码”需保持关闭。
- **浏览器**：镜像内已带 Chromium，只能用独立后台浏览器；“复用已登录 Chrome”的扩展模式需要与用户桌面同机，线上不可用。

应用密钥或 Web 模型配置变化时，只更新 `shared/.env` / `shared/runtime_config.json`，再通过受控流程重建当前容器；不要把密钥写进仓库、镜像、命令输出或 release state。
