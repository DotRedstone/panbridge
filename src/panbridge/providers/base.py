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

    async def get_share_download_info(
        self, share_id: str, password: str | None, file_id: str
    ) -> dict[str, Any]:
        """解析分享中单个文件的下载信息（匿名，无需登录）。

        与 `get_download_info` 的区别：后者下载「账号网盘中的文件」
        （需要真实 fid 与登录态），本方法下载「分享中的文件」，
        依赖分享上下文（share_id/password 及 Provider 内部解析出的
        token/pass_token/etag/s3keyFlag 等）。

        返回字典应包含 `url`（必填）与可选 `name`/`size`/`checksum`/
        `headers`。默认抛 UNSUPPORTED_OPERATION —— 表示该平台不支持
        匿名分享直链下载（需先转存再下载）。

        实现注意：Provider 实例是无状态的，本方法应在需要时重新解析
        分享上下文（token 等），不得依赖跨方法调用的实例状态。
        """
        from panbridge.domain.errors import DomainError, ErrorCode

        raise DomainError(
            ErrorCode.UNSUPPORTED_OPERATION,
            f"provider {self.name} does not support anonymous share download",
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

    async def create_folder(
        self, account: Account, name: str, parent_id: str = "0"
    ) -> str:
        """在账号网盘创建目录，返回新目录的 fid。

        默认不支持（UNSUPPORTED_OPERATION）。支持转存到独立目录的平台
        应实现此方法，避免转存污染根目录。
        """
        from panbridge.domain.errors import DomainError, ErrorCode

        raise DomainError(
            ErrorCode.UNSUPPORTED_OPERATION,
            f"provider {self.name} does not support creating folders",
        )


def provider_supports(provider: ShareProvider, capability: str) -> bool:
    return capability in provider.capabilities
