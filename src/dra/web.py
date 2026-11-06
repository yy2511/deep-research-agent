"""研究工作台启动入口（兼容旧命令与部署配置）。

``uvicorn dra.web:app`` 与 ``uv run python -m dra.web`` 继续可用；
应用的组装在 dra.api.app，接口在 dra.api.routers，运行管理在 dra.api.services。
用法：uv run python -m dra.web  → http://localhost:8765
"""
from dra.api.app import app, main

__all__ = ["app", "main"]

if __name__ == "__main__":
    main()
