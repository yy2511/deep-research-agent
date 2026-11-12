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
