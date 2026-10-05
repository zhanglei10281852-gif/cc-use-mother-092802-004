"""领域错误类型。

所有业务校验失败都抛出 DomainError 子类，API 层据此映射 HTTP 状态码，
保证同一类问题在任何入口（服务调用 / HTTP）下行为一致。
"""


class DomainError(Exception):
    """业务错误基类。"""

    code = "domain_error"
    http_status = 400


class ValidationError(DomainError):
    """输入不合法（时间未对齐、参数缺失、数值非法等）。"""

    code = "validation_error"
    http_status = 400


class NotFoundError(DomainError):
    """引用的实体不存在。"""

    code = "not_found"
    http_status = 404


class ConflictError(DomainError):
    """与既有数据冲突（重复编号、重复报送、重复签发等）。"""

    code = "conflict"
    http_status = 409


class ImmutableError(ConflictError):
    """试图修改或删除已签发的结论。"""

    code = "immutable"
    http_status = 409
