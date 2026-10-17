"""Provider 抽象层。

定义统一的网盘能力接口。每个平台在独立目录中实现，
通过 registry 注册。适配器不得把平台私有字段泄漏给外部 API。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from panbridge.domain.models import (
    Account,
    DownloadArtifact,
    Share,
    ShareFile,
    TransferResult,
)


class ShareProvider(ABC):
    """统一网盘适配器接口。

    方法按能力分组，平台不支持的能力应抛出
    UNSUPPORTED_OPERATION 错误，不得伪造成功结果。
    """

    name: str
    display_name: str
    capabilities: frozenset[str]

    @abstractmethod
    async def inspect_share(self, share_id: str, password: str | None) -> Share:
        """解析分享，返回分享标题与文件列表。"""

    @abstractmethod
    async def list_share_files(
        self, share_id: str, password: str | None, parent_id: str | None = None
    ) -> list[ShareFile]:
        """列出分享中的文件/目录。"""

    @abstractmethod
    async def validate_account(self, account: Account) -> bool:
        """校验账号凭据是否有效。"""

    @abstractmethod
    async def save_shared_files(
        self,
        account: Account,
        share_id: str,
        password: str | None,
        file_ids: list[str],
        destination: str | None,
        on_conflict: str,
    ) -> TransferResult:
        """将分享中的指定文件转存到账号网盘。"""

    @abstractmethod
    async def list_account_files(
        self, account: Account, parent_id: str | None = None
    ) -> list[ShareFile]:
        """列出账号网盘中的文件。"""

    @abstractmethod
    async def get_download_info(
        self, account: Account, file_id: str
    ) -> dict[str, Any]:
        """获取文件下载信息（URL、大小、校验信息等）。"""

    # ---- 以下为可选能力，提供默认实现以保持接口稳定 ----

    async def download_file(
        self, account: Account, file_id: str, local_path: str
    ) -> DownloadArtifact:
        """平台自带下载（默认不支持，交由通用下载层）。"""
        from panbridge.domain.errors import DomainError, ErrorCode

        raise DomainError(
            ErrorCode.UNSUPPORTED_OPERATION,
            f"provider {self.name} does not support direct download",
        )

    async def resolve_saved_file_ids(
        self, account: Account, destination: str | None, names: list[str]
    ) -> list[str]:
        """转存完成后解析新文件 ID。

        部分平台（如夸克）转存是异步的，文件在账号网盘中获得新 fid，
        下载必须用新 fid。默认实现返回空列表，表示「无此需求」——
        调用方应根据平台能力决定是否需要此步骤。
        """
        return []


def provider_supports(provider: ShareProvider, capability: str) -> bool:
    return capability in provider.capabilities
