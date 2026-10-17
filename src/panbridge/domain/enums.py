"""领域枚举定义。

这些枚举属于领域语言，不依赖框架或存储实现。
"""

from __future__ import annotations

from enum import StrEnum


class Action(StrEnum):
    """任务业务模式。"""

    INSPECT = "inspect"  # 仅解析和预览分享
    SAVE = "save"  # 仅转存到自己的网盘
    DOWNLOAD = "download"  # 直接下载
    SAVE_AND_DOWNLOAD = "save_and_download"  # 先转存再下载


class Capability(StrEnum):
    """平台能力描述。

    用明确的能力声明代替「假设每个平台功能一致」。
    """

    INSPECT_SHARE = "inspect_share"
    LIST_SHARE_FILES = "list_share_files"
    SAVE_SHARED_FILES = "save_shared_files"
    LIST_ACCOUNT_FILES = "list_account_files"
    GET_DOWNLOAD_INFO = "get_download_info"
    VALIDATE_ACCOUNT = "validate_account"


class TaskStatus(StrEnum):
    """任务生命周期状态。"""

    QUEUED = "queued"
    RESOLVING = "resolving"
    SAVING = "saving"
    DOWNLOADING = "downloading"
    WAITING_AUTH = "waiting_auth"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStep(StrEnum):
    """任务内部阶段，用于进度与失败定位。"""

    EXTRACT_LINK = "extract_link"
    IDENTIFY_PROVIDER = "identify_provider"
    RESOLVE_SHARE = "resolve_share"
    SAVE_FILES = "save_files"
    DOWNLOAD_FILES = "download_files"
    FINALIZE = "finalize"


class OnConflict(StrEnum):
    """转存目标存在同名文件时的冲突策略。"""

    SKIP = "skip"  # 跳过已存在
    RENAME = "rename"  # 自动重命名
    OVERWRITE = "overwrite"  # 覆盖（平台支持时）
