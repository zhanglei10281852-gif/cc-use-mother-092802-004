"""领域错误类型。"""


class AuditError(Exception):
    """所有核证领域错误的基类。"""

    code = "audit_error"


class ValidationError(AuditError):
    code = "validation_error"


class NotFoundError(AuditError):
    code = "not_found"


class ConflictError(AuditError):
    """同一 ID 重复提交但内容不一致，或区间重叠等确定性冲突。"""

    code = "conflict"


class ImmutableError(AuditError):
    """试图修改已签发的不可变结论。"""

    code = "immutable_conclusion"
