# 部署与发布

> 本文只描述当前可执行的部署合同。当前版本、验证结果和未复核项以 [`../STATUS.md`](../STATUS.md) 为准。发布脚本与 CI 于 2026-10-26 从迁出基准 `feat/skills@85f400b` 恢复，并按现役镜像（含浏览器）调整。

## 当前拓扑

```text
GitHub Actions（yy2511/deep-research-agent，main 分支）
  → 后端测试 + 执行沙箱实测 + 前端 test/lint/build
  → 构建并 smoke test linux/amd64 镜像
  → 私有 GHCR：sha-<完整 Git SHA>@sha256:<digest>
  → 人工 stage → migrate → verify → activate
  → Docker Compose（127.0.0.1:8765）
  → Caddy HTTPS + 应用账号登录
  → https://deepresearch.emberecho.de
```

Caddy 是唯一公网入口，应用容器只绑定 loopback。应用账号负责登录、研究归属和管理员权限。未登录访问私人研究与设置接口返回 401，首页、健康检查与公开示例按各自接口合同开放。线上私域资料库由 Caddy 返回 503，镜像中也没有部署本地检索依赖和嵌入模型。

发布到 GHCR **不等于部署**。`main` 更新后生产不会自动拉取或重启，必须使用受控发布脚本切换。2026-08 的旧代码线保留在 `dag-v1` 分支，不再触发构建。

## 状态与密钥边界

生产根目录默认为 `/home/deploy/apps/deep-research-agent`：

```text
shared/
  .env                 应用密钥，0600
  runtime_config.json  Web 模型配置
  runs/                Web 运行记录
  plans/               Planner 尝试与诊断
database/
  compose.yaml         独立 PostgreSQL 16 项目
  postgres.env         数据库账号密码，0600
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

1. `uv sync --locked` 后运行 `uv run pytest`（每个测试用临时 SQLite），再用 `--run-postgres` 在 PostgreSQL 服务上把同一批测试跑一遍，然后构建执行沙箱镜像并跑真实 Docker 执行测试；
2. `npm ci` 后运行 `npm test`、`npm run lint`、`npm run build`；
3. 非 PR 事件构建 `linux/amd64` 镜像并发布完整 SHA tag；
4. 从 GHCR 拉回镜像，在临时 Docker 网络里起一个 PostgreSQL，用镜像执行 `python -m dra.admin migrate`，再带 `DRA_DATABASE_URL` 启动，检查架构、`/healthz`、首页、非 root 用户、生产依赖；浏览器所需的 Node、Playwright MCP 和 Chromium 必须存在，npm、uv、`.env` 和前端源码不应存在；
5. 全部通过后，`main` 分支的构建才把 `main` 便利 tag 指向同一 digest。

服务器版本必须使用完整 `sha-<40 位 Git SHA>` tag 和 digest，不能使用会移动的便利 tag。服务器只保存 GHCR 只读凭据；Actions 使用任务级 `GITHUB_TOKEN` 发布。

## 同机 PostgreSQL

用户于 2026-11-12 选定先用现有服务器上的 Docker PostgreSQL，用户增加后再考虑托管数据库。

将 `deploy/compose.postgres.yaml` 安装为生产根目录下的 `database/compose.yaml`，在同目录创建权限为 0600 的 `postgres.env`，填写 `POSTGRES_USER`、`POSTGRES_DB` 和随机生成的 `POSTGRES_PASSWORD`。应用的 `shared/.env` 配置 `DRA_DATABASE_URL=postgresql+psycopg://<user>:<password>@postgres:5432/<database>`，实际密码只写受控运行配置。

```bash
docker compose --file /home/deploy/apps/deep-research-agent/database/compose.yaml up -d --wait
```

将 `deploy/compose.production.yaml` 安装为 `container/compose.yaml`。数据库通过独立持久卷保存数据，网络名为 `deep-research-agent-database`，不发布宿主机端口。生产应用与 canary 都连接该网络里的 `postgres`，同时各自保留出站网络以访问模型和检索服务。应用发布、canary 清理与回滚只操作应用项目。

生产 Compose 信任 Caddy 传来的转发头，使 HTTPS 回调地址和 Secure Cookie 使用正确协议。此配置依赖应用端口始终只发布到 `127.0.0.1`。

GitHub OAuth App 的主页为 `https://deepresearch.emberecho.de`，回调为 `https://deepresearch.emberecho.de/api/auth/github/callback`。将 Client ID 和 Client secret 写入 `shared/.env` 的 `DRA_GITHUB_CLIENT_ID` 与 `DRA_GITHUB_CLIENT_SECRET`。

首次跨账号版本切换时，旧版无法读取新增的 `trial` 字段。先在受控位置保存目标配置，以切换前的兼容配置启动新版，再通过管理员设置接口保存目标试用设置。这样初次激活失败时，原有回退流程仍可启动旧版。

首次切换完成后，停止新 Web 服务，创建管理员并执行 `python -m dra.admin import-legacy --owner <管理员邮箱>`，再启动新 Web 服务。导入期间保持 Web 停止，避免旧研究归属登记和旧资料迁移与网页请求同时进行。确认账号、历史和权限可用后，按 `deploy/Caddyfile.dra` 更新本站配置，用应用账号接管登录。

## 数据库备份

用户已选定每天备份并持续保留。`deploy/postgres-backup.sh` 使用 PostgreSQL 自带的 `pg_dump -Fc`，将完整备份写入 `backups/postgres-<UTC时间>.dump`。成功写完后才改为正式文件名。部署时先手动执行并用 `pg_restore --list` 检查，再设北京时间每日 03:20 的任务。备份目录和文件由部署账号保管。

```bash
sh /home/deploy/apps/deep-research-agent/container/postgres-backup.sh
```

恢复时通过 PostgreSQL 容器里的 `pg_restore` 将选定备份导入目标库。首次导入管理员与历史后，需要另做一次备份。首次跨越账号版本回滚时，旧版还没有应用账号鉴权。先从 `backups/Caddyfile-pre-phase2-*` 恢复本站 Caddy Basic Auth 并校验、重新加载，再从 `backups/pre-phase2-20261112T081848Z.tar.gz` 恢复 `shared/runtime_config.json`，随后执行镜像 rollback。数据库卷继续保留。切换前的 Caddy 备份包含同机站点配置，恢复前应核对其他站点在此后是否有更新。

## 生产发布与回滚

发布前从成功的 Actions run 取得完整 tag 和 digest，然后在生产机执行：

```bash
cd /home/deploy/apps/deep-research-agent/container

DRA_MAX_ACTIVE_RUNS=2 ./container_release.py stage \
  ghcr.io/yy2511/deep-research-agent:sha-<40位Git-SHA> \
  sha256:<64位镜像摘要>

./container_release.py migrate <40位Git-SHA>
./container_release.py verify <40位Git-SHA>
./container_release.py status
./container_release.py activate <40位Git-SHA>
```

各阶段含义：

- `stage`：校验不可变坐标、拉取镜像并记录 release；
- `migrate` 使用 staged 镜像在一次性容器中执行数据库迁移，执行完退出。
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

本地 `.env` 和持久目录必须先存在；`.env` 里的 `DRA_DATABASE_URL` 必须是容器内能访问的地址（容器里的 `127.0.0.1` 指容器自己，访问宿主机上的 `compose.dev-db.yaml` 数据库要用宿主机地址），并已执行过迁移。`compose.yaml` 设置 `create_host_path: false`，路径拼错时会明确失败，避免 Docker 静默创建 root 所有的空目录。

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
- `data/runs/plans` 三个持久目录可创建并清理探针文件；
- 数据库可连接（`SELECT 1`）。

它不读取模型配置，也不调用 LLM、搜索或研究编排。容器内部正常返回 200，持久目录不可写或数据库连不上时返回 503。数据库缺失、不是 PostgreSQL 或迁移不是最新时，Web 进程直接拒绝启动。公开试用上线后，公网 `/healthz` 返回 200。

一次生产切换至少核对：

1. `container_release.py status` 无 pending，active revision 与目标 SHA 一致；
2. Compose 容器为 `healthy` 且 restart count 未增加；
3. loopback 首页和 `/healthz` 返回 200；
4. 公网首页与 `/healthz` 返回 200，匿名访问 `/api/me` 与 `/api/runs` 返回 401，管理员登录后能查看自己的历史；
5. `runtime_config.json`、历史 run 和 shared 文件在切换前后保持；
6. 其他同机服务不受影响。

## 当前未覆盖

- **数据库与账号**使用上面的生产专用 Compose 和迁移流程。本机 `compose.dev-db.yaml` 继续用于开发。
- **试用**初始值为 OpenCode 连接下的 `deepseek-v4.1-flash`，每天研究个人 2 次、全部访客 20 次，规划 10/100 次，访客并发 1，单次 900 秒、模型与工具各 60 次，北京时间零点重置。管理员可在设置页修改。全站并发为 2，包含管理员和访客。
- **私域资料库**由本站 Caddy 对 `/api/library` 及其子路径返回 503，访客在应用层也受现有权限控制。管理员界面可能仍显示资料入口，本次部署未安装本地检索依赖与模型。
- **线上运行代码**：应用跑在容器里，研究中的 Bash 需要宿主机 Docker 和执行沙箱镜像（`compose.execution.yaml`）。`container_release.py` 目前只使用单个 `compose.yaml`，不带这份叠加文件，所以按本流程发布的线上实例不能运行代码，网页“运行代码”需保持关闭。
- **浏览器**：镜像内已带 Chromium，只能用独立后台浏览器；“复用已登录 Chrome”的扩展模式需要与用户桌面同机，线上不可用。

应用密钥或 Web 模型配置变化时，只更新 `shared/.env` / `shared/runtime_config.json`，再通过受控流程重建当前容器；不要把密钥写进仓库、镜像、命令输出或 release state。

## 四种身份与用户管理升级（迁移 0006）

此升级不自动指定负责人。升级前停旧服务写入并备份数据库；运行现有迁移命令，再启动新版，不能混跑旧版与新版。旧普通用户保留不限次政策，访客仍使用全站通用试用配置。

部署维护者需要初始化负责人时，先只读列出账号并核对身份，再显式运行初始化：

```sh
uv run python -m dra.admin list-users
uv run python -m dra.admin bootstrap-owner --user-id <已核对的管理员ID>
```

命令显示目标身份/角色/状态并要求确认；目标必须是有效管理员，已存在负责人时拒绝初始化。不要自动选择最早账号或仓库所有者。初始化后目标旧会话撤销，需要重新登录。`--yes` 只适用于维护者已在别处完成核对的脚本，并非跳过服务端约束。

常规移交由当前负责人在“负责人管理”重新认证并独立确认。紧急恢复使用 `recover-owner --old-owner-id <原ID> --user-id <目标ID> --reason <恢复原因>`，同样先核对身份并审计。不要使用通用 `set-role` 绕过负责人治理。

迁移的降级操作会拒绝自动删除新增的治理、额度与审计数据。需要回退时先备份并制定人工恢复方案，不能直接部署不认识 owner 角色的旧应用。
