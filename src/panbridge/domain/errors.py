"""领域错误与统一错误码。

所有对外暴露的错误都映射为稳定的错误码，
不应把上游 Cookie、Token、traceback 等敏感信息泄漏给调用方。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """稳定的对外错误码。"""

    INVALID_SHARE = "INVALID_SHARE"
    INVALID_PASSWORD = "INVALID_PASSWORD"
    INVALID_REQUEST = "INVALID_REQUEST"
    UNSUPPORTED_PROVIDER = "UNSUPPORTED_PROVIDER"
    UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION"
    ACCOUNT_EXPIRED = "ACCOUNT_EXPIRED"
    ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    RATE_LIMITED = "RATE_LIMITED"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    TASK_NOT_CANCELLABLE = "TASK_NOT_CANCELLABLE"
    TASK_NOT_RETRYABLE = "TASK_NOT_RETRYABLE"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    UNAUTHORIZED = "UNAUTHORIZED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class DomainError(Exception):
    """领域层统一错误基类。

    code     —— 稳定的错误码（见 ErrorCode）。
    message  —— 面向调用方的安全描述。
    retryable—— 是否建议重试。
    details  —— 结构化的额外信息，不含敏感数据。
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}
