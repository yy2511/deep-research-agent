"""对外公开的 ID 格式（运行、对话、追问请求）。业务层和接口层共用。"""
from __future__ import annotations

import re

PUBLIC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
RUN_ID_RE = re.compile(PUBLIC_ID_PATTERN)
