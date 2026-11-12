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
