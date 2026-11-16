# EviSee 界面视觉（#73）

## 依据与范围

依据用户提供的[品牌讨论与 Logo](https://chatgpt.com/s/cx_6afac3dcaf408191a5ff661782e0a0d1)，采用最终选定的 **EviSee** 名称和 **See the evidence. Check the source.** 标语。图形以绿色证据卡、字母 E 和核验勾为核心；原始参考图保存在 [evisee-logo.png](../../web/public/branding/evisee-logo.png)，界面与 favicon 使用可缩放 SVG。

本次基于 PR #67 的用户管理实现，以及已包含的运营概览改造视觉，不改变后端、数据库迁移、权限、额度、研究控制流或引用合同。测试预览与原仓库生产部署相互独立。

## 视觉与交互

- 深绿 `#08745B` 表示主操作、当前导航和证据入口；白色表面与浅绿灰背景分隔内容。警告、失败和停用继续使用独立状态色，并保留文字说明。
- 使用系统无衬线字体，不请求外部字体；统一控件高度、圆角、焦点轮廓和留白，尊重减少动态效果偏好。
- 登录页在桌面呈现品牌介绍与表单双栏，窄屏保留紧凑表单。研究首页突出问题输入、来源选择与可点击的示例问题。
- 主侧栏显示研究空间、研究历史与账号。设置按平台管理、研究配置、个人偏好分组；平板与手机使用可横向滚动的分类导航。
- 运营概览先呈现所选期间的四项活动，再呈现不受日期影响的账号总览。趋势图仅绘制接口返回的每日研究任务和研究用户，保留 UTC 日期、计数口径、零值与完整表格。
- 用户管理将账号身份、状态、额度来源和使用量明确分列；窄屏保留带字段标签的账号卡片。详情弹窗可以滚动，保留关闭、Escape、焦点返回及独立确认区域。
- 负责人管理呈现当前负责人和核对账号、验证身份、确认移交三步。验证与最终确认仍是原来的独立操作。
- 研究正文、证据、原文和调用详情沿用现有结构，统一字体、绿色引用、证据卡与原文高亮的视觉。

## 验证与截图

真实后端预览使用隔离 PostgreSQL 测试库与合成账号。截图不含密码、会话或密钥。旧版桌面与手机共 14 张基线留在测试环境；新版额外覆盖 1024 像素平板。下列截图为实际浏览器渲染，页面中的账号与活动数仅是测试数据。

| 页面 | 桌面 | 手机 |
| --- | --- | --- |
| 登录 | [截图](assets/evisee/desktop-login.png) | [截图](assets/evisee/mobile-login.png) |
| 研究首页 | [截图](assets/evisee/desktop-home.png) | [截图](assets/evisee/mobile-home.png) |
| 运营概览 | [截图](assets/evisee/desktop-operations.png) | [截图](assets/evisee/mobile-operations.png) |
| 用户管理 | [截图](assets/evisee/desktop-users.png) | [截图](assets/evisee/mobile-users.png) |
| 用户详情 | [截图](assets/evisee/desktop-user-dialog.png) | [截图](assets/evisee/mobile-user-dialog.png) |
| 负责人管理 | [截图](assets/evisee/desktop-owner.png) | [截图](assets/evisee/mobile-owner.png) |

旧版对照：[研究首页](assets/evisee/before-desktop-home.png)、[用户管理](assets/evisee/before-desktop-users.png)。另有[平板用户管理](assets/evisee/tablet-users.png)、[模型设置](assets/evisee/desktop-settings-models.png)和[手机趋势图](assets/evisee/mobile-operations-trend.png)。趋势图在三个断点的坐标文字实际均为 11px；[几何与交互报告](assets/evisee/browser-checks.json)记录实际结果。

研究视图使用独立内存合成数据验收：[报告正文](assets/evisee/desktop-report.png)、[证据详情](assets/evisee/desktop-evidence.png)、[手机原文定位](assets/evisee/mobile-source.png)、[验收报告](assets/evisee/research-checks.json)。报告引用、证据搜索与切换、关闭后的焦点返回、原文精确高亮及返回入口均通过；无真实 API 或模型调用。入口与运行方式见 [QA 说明](../../web/qa/README.md)。

`web/scripts/check-interface-browser.mjs` 在 loopback 地址读取运行时凭据，覆盖登录页面、首页、模型设置、运营筛选、用户筛选、详情弹窗 Escape/焦点返回与负责人导航。浏览器页面仅允许同机只读 API；登录由共享 Cookie 的请求上下文完成，不修改账号或研究数据。检查浏览器错误、HTTP 错误、意外外部请求、写入请求及页面横向溢出。

`web/qa/check-user-management.mjs` 使用纯内存 API 夹具，额外覆盖保存与取消、重复确认、403/409、负责人验证及取消。它不连接真实后端，不能替代服务端权限或数据库事务验收。

实际验证：`npm --prefix web test` 为 33 文件 / 253 passed；`npm --prefix web run lint` 为 0 errors / 13 条既有 warnings；`npm --prefix web run build` 通过，沿用单包大于 500 kB 的提醒。`uv run pytest -q -s tests/test_operations_browser.py --run-browser` 在独立临时 SQLite 与数据目录中为 1 passed；真实 PostgreSQL 预览另通过四角色 32 项 HTTP 检查。没有将内存夹具或临时测试数据当成生产数据验收。

## 边界

本次没有新增或移除数据大小、数量、格式及研究预算限制；原有角色、额度、来源访问与确认规则保持。未调用真实模型、付费搜索、GitHub OAuth 或 Docker，也未对原仓库生产环境发布。临时公网预览沿用当前隧道，实时研究事件可能被隧道缓冲；普通页面和管理 HTTP 请求已验证。

本次为既有界面的视觉实现，不修改已定 ADR。
