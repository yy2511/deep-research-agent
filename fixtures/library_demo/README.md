# 私域 RAG 示例包

本目录用于没有工作文档时体验文件研究。不是企业真实资料。

- `orchid-requirements.md`、`orchid-operations.txt`、`orchid-data-policy.pdf`：人工编写的模拟需求，包含内部编号、43 天日志 / 17 天备份等可对拍事实，以及明确无答案项。
- `python-3.13-asyncio-task.txt`：CPython `v3.13.0` 的官方文档源码，保留 reStructuredText 字节，不转换为 Markdown，不代表 Python 最新版本。
  原始地址：<https://raw.githubusercontent.com/python/cpython/v3.13.0/Doc/library/asyncio-task.rst>
  下载日期：2026-09-19。许可证见 `PYTHON-LICENSE`，来源为同一标签的 `LICENSE`。
- `manifest.json`：文件 SHA-256 与来源，用于复跑核对。

建议选中四份资料，选择“仅资料”：

1. Orchid 的日志和备份分别保留多久？给出文件依据。
2. 单文件容量目标与本地 embedding 约束是什么？
3. 对照 Python 3.13 文档，Orchid 能否把等待超时当成后台任务已退出？
4. GPU 采购预算是多少？（材料未提供，期望说明缺口。）

“试查”只运行本机检索，不调用研究模型。完整研究使用界面当前配置的模型。
