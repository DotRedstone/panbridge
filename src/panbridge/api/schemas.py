"""API 请求/响应模型（Pydantic v2）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class InspectFile(BaseModel):
    file_id: str
    name: str
    is_dir: bool = False
    size: int | None = None


class InspectResponse(BaseModel):
    provider: str
    share_id: str
    title: str
    files: list[InspectFile]


class InspectRequest(BaseModel):
    text: str


class TaskCreateRequest(BaseModel):
    input: dict[str, Any] = Field(
        default_factory=dict,
        description="包含 text/action/account_id/selection/destination/on_conflict",
    )


class TaskResponse(BaseModel):
    id: str
    action: str
    status: str
    provider: str | None = None
    account_id: str | None = None
    selection: list[str] | None = None
    destination: str | None = None
    on_conflict: str
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    attempt: int = 0
    max_attempts: int = 3
    result: dict[str, Any] | None = None
    created_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None


class TaskListResponse(BaseModel):
    items: list[TaskResponse]


class AccountCreateRequest(BaseModel):
    provider: str
    label: str = ""
    credential: str


class AccountResponse(BaseModel):
    id: str
    provider: str
    label: str
    created_at: str | None = None
    updated_at: str | None = None


class QrLoginStartResponse(BaseModel):
    session_id: str
    provider: str
    qr_url: str


class QrLoginStatusResponse(BaseModel):
    session_id: str
    status: str  # waiting | scanned | success | expired | cancelled | failed
    account_id: str | None = None
    account: AccountResponse | None = None


class QrCancelResponse(BaseModel):
    session_id: str
    cancelled: bool


class LoginStatusResponse(BaseModel):
    logged_in: bool
    expired: bool = False
    account: AccountResponse | None = None


class LogoutResponse(BaseModel):
    provider: str
    removed: int


class TaskFileResponse(BaseModel):
    name: str
    path: str
    size: int | None = None


class TaskFilesResponse(BaseModel):
    items: list[TaskFileResponse]
