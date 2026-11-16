# 用户管理离线浏览器验收

此夹具直接导入实际 `UserManagement` 和 `OwnerManagement` 组件，但全部 API 都由内存模拟响应提供。不连接后端、不使用真实账号/密码、不验证后端权限或事务。

```sh
npm --prefix web ci
npx --prefix web vite build --config web/qa/vite.config.ts
python -m http.server 8766 --bind 127.0.0.1 --directory ../user-management-qa-dist
```

打开 `http://127.0.0.1:8766/user-management.html`。角色/页面切换与 403、409 注入按钮只是测试工具，不进入生产构建。负责人密码验证可填任何合成字符串，不能填真实密码。

检查桌面及窄屏布局、账号筛选、详情打开关闭/取消、额度确认、重复点击、管理权限失效、冲突重载，以及负责人移交的独立确认。成功写入次数显示在顶部。重新加载页面恢复初始合成数据。

这些测试不能代替数据库迁移、服务端权限、并发配额和真实 OAuth 验收；后端测试位于 `tests/test_user_management.py`、`tests/test_user_quotas.py` 等。

## 2026-11-12 本机验收记录

LAPTOP-DD 上使用已安装 Google Chrome headless 与 Playwright，源码基线 `950a84e` 加本次移动标题/主题变量修复。锁定依赖安装、QA Vite 构建成功。桌面 1440×1100、移动 390×844 均通过，最终样式 SHA256 为 `97fa43a9057a4979611a60c5693ce6ba6ab10a6e196c59a1d3664527d9961c6f`。

覆盖：列表和搜索过滤、管理员权限限制、访客通用额度只读、额度取消/保存与重复点击、403 清理/409 提示、负责人重新验证和取消、关闭/Escape、无横向溢出、移动 caption 横排几何断言；无 JavaScript 异常。实际执行入口为 `check-user-management.mjs`，通过环境变量指定本机 localhost URL、Chrome 可执行文件和截图目录。

像素检查发现移动表格 caption 逐字竖排，以及管理页引用未定义颜色变量，均在本次修复后重建、重跑并重新截图。

[截图目录](../../docs/design/assets/user-management/) 包含五张只使用合成账号的真实浏览器截图。仍不是生产或真实后端端到端验收；负责人密码为合成字符串，未访问真实账号/API。

## 研究视图离线视觉验收

`research-workspace.html` 独立导入真实 `ConversationWorkspace`、品牌组件及完整主题 CSS。报告、证据卡、来源与原文全部为明确标注的合成数据；夹具在渲染前将 `fetch` 替换为内存响应，拒绝未知请求、外部请求与非 GET 请求，不调用模型或真实 API。它不进入产品入口，也不替代后端、权限或真实模型研究质量测试。

依赖已安装后，在仓库根目录启动仅监听本机的 QA 开发服务（此研究夹具不在现有管理 QA 的静态构建入口中）：

```sh
cd web
node node_modules/vite/bin/vite.js --config qa/vite.config.ts --host 127.0.0.1 --port 5197 --strictPort
```

打开 `http://127.0.0.1:5197/research-workspace.html`，或另开终端从仓库根目录运行浏览器验收。`DRA_BROWSER_EXECUTABLE` 指向本机已安装的 Chromium；未设置则使用 Playwright 已安装的浏览器，始终启用 Chromium sandbox。

```sh
cd web
DRA_RESEARCH_QA_URL=http://127.0.0.1:5197/research-workspace.html \
DRA_BROWSER_EXECUTABLE=/usr/bin/chromium \
DRA_QA_SCREENSHOTS=/tmp/evisee-research-qa \
node qa/check-research-workspace.mjs
```

脚本使用桌面 1440×1000、手机 390×844，覆盖报告引用、证据列表与搜索、详情关闭后的焦点恢复、上下文与保存全文的精确高亮、定位后返回入口完整可见、视图切换及页面/研究区无横向溢出。浏览器层同时拦截真实 API 和外部网络请求，并检查脚本异常与读取失败提示。截图和 `result.json` 写入指定的私有本机目录，验收结束后关闭 QA 服务。
