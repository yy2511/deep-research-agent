"""业务层的错误类型。

业务层只说明"出了什么问题"，不决定 HTTP 状态码；接口层用 ``dra.api.common.service_errors``
把它们翻译成状态码。这样同一段业务逻辑既能被接口调用，也能被以后的 Worker 进程直接调用。
"""
from __future__ import annotations


class ServiceError(Exception):
    """业务层错误的基类；``str(exc)`` 是给用户看的中文说明。"""


class Unauthorized(ServiceError):
    """没有登录，或登录凭据不对。接口层对应 401。"""


class Forbidden(ServiceError):
    """已识别身份，但不允许这个操作（如账号已停用）。接口层对应 403。"""


class NotFound(ServiceError):
    """要操作的对象不存在。接口层对应 404。"""


class InvalidRequest(ServiceError):
    """请求内容不符合业务规则。接口层对应 400。"""


class Conflict(ServiceError):
    """对象当前的状态不允许这个操作。接口层对应 409。"""


class Unavailable(ServiceError):
    """依赖的存储或服务暂时不可用，没有确认操作成功。接口层对应 503。"""


class CapacityFull(ServiceError):
    """同时运行的研究已达上限。接口层对应 429。"""
