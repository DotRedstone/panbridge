"""领域层：独立于框架的核心领域对象。

本层不依赖 FastAPI、SQLAlchemy 或任何具体网盘 SDK，
只定义业务概念、枚举和错误类型。
"""

from panbridge.domain.enums import (
    Action,
    Capability,
    OnConflict,
    TaskStatus,
    TaskStep,
)
from panbridge.domain.errors import (
    DomainError,
    ErrorCode,
)
from panbridge.domain.models import (
    Account,
    DownloadArtifact,
    Provider,
    Share,
    ShareFile,
    Task,
    TransferResult,
)
from panbridge.domain.models import (
    Capability as CapabilitySet,
)

__all__ = [
    "Action",
    "Account",
    "Capability",
    "CapabilitySet",
    "DownloadArtifact",
    "DomainError",
    "ErrorCode",
    "OnConflict",
    "Provider",
    "Share",
    "ShareFile",
    "Task",
    "TaskStep",
    "TaskStatus",
    "TransferResult",
]
