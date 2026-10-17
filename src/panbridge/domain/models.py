"""领域数据对象。

这些是纯 Python 数据对象（dataclass），不依赖 ORM 或 Web 框架。
持久化层负责在领域对象与数据库行之间转换。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from panbridge.domain.enums import (
    Action,
    Capability,
    OnConflict,
    TaskStatus,
)


@dataclass(frozen=True)
class Provider:
    """网盘提供商描述。"""

    name: str
    display_name: str
    capabilities: frozenset[Capability] = field(default_factory=frozenset)


@dataclass
class Account:
    """已配置的网盘账号。"""

    id: str
    provider: str
    label: str
    # 凭据不直接以明文形式出现在领域对象中，由基础设施层加解密。
    encrypted_credential: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class ShareFile:
    """分享中的单个文件或目录。"""

    file_id: str
    name: str
    is_dir: bool = False
    size: int | None = None
    parent_id: str | None = None


@dataclass
class Share:
    """解析后的分享内容。"""

    provider: str
    share_id: str
    title: str
    files: list[ShareFile] = field(default_factory=list)
    password: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class DownloadArtifact:
    """一次下载产出的本地文件。"""

    source_file_id: str
    name: str
    local_path: str
    size: int
    checksum: str | None = None


@dataclass
class TransferResult:
    """转存结果。"""

    saved_file_ids: list[str] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    # 远端任务标识，用于幂等确认，降低重复转存风险。
    remote_task_id: str | None = None


@dataclass
class Task:
    """持久化任务（领域视图）。"""

    id: str
    action: Action
    status: TaskStatus = TaskStatus.QUEUED
    provider: str | None = None
    account_id: str | None = None
    input_text: str | None = None
    selection: list[str] | None = None
    destination: str | None = None
    on_conflict: OnConflict = OnConflict.SKIP
    idempotency_key: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    attempt: int = 0
    max_attempts: int = 3
    # 任务最终结果（JSON 序列化），如已下载文件路径、转存结果等。
    result: dict[str, Any] | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None
