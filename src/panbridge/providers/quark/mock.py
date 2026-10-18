"""Mock Provider：用于无真实账号环境下验证完整任务链路。

模拟以下场景：
- 有效分享 / 无效分享 / 无效提取码
- 转存成功 / 账号失效 / 下载失败

通过 share_id 的前缀控制行为，便于测试与演示。
"""

from __future__ import annotations

from typing import Any

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.domain.models import (
    Account,
    Share,
    ShareFile,
    TransferResult,
)
from panbridge.providers.base import ShareProvider


class MockProvider(ShareProvider):
    name = "mock"
    display_name = "Mock Provider"
    capabilities = frozenset(
        {
            "inspect_share",
            "list_share_files",
            "save_shared_files",
            "list_account_files",
            "get_download_info",
            "get_share_download_info",
            "validate_account",
            "create_folder",
        }
    )

    async def inspect_share(self, share_id: str, password: str | None) -> Share:
        if share_id.startswith("invalid"):
            raise DomainError(
                ErrorCode.INVALID_SHARE,
                "share does not exist or has been removed",
            )
        if share_id.startswith("needpass") and password != "1234":
            raise DomainError(
                ErrorCode.INVALID_PASSWORD,
                "invalid extraction password",
            )
        files = self._sample_files(share_id)
        return Share(
            provider=self.name,
            share_id=share_id,
            title=f"mock-share-{share_id}",
            files=files,
            password=password,
        )

    async def list_share_files(
        self, share_id: str, password: str | None, parent_id: str | None = None
    ) -> list[ShareFile]:
        # 支持按 parent_id 返回子目录内容，用于测试目录递归遍历。
        prefix = share_id
        if parent_id == f"{prefix}-d1":
            # d1 目录下有一个子目录 d1a 和一个文件 f3
            return [
                ShareFile(
                    file_id=f"{prefix}-d1a", name="sub", is_dir=True,
                    parent_id=f"{prefix}-d1",
                ),
                ShareFile(
                    file_id=f"{prefix}-f3", name="deep.txt", size=32,
                    parent_id=f"{prefix}-d1",
                ),
            ]
        if parent_id == f"{prefix}-d1a":
            # d1a 目录下有一个文件 f4
            return [
                ShareFile(
                    file_id=f"{prefix}-f4", name="deepest.bin", size=16,
                    parent_id=f"{prefix}-d1a",
                ),
            ]
        share = await self.inspect_share(share_id, password)
        return share.files

    async def validate_account(self, account: Account) -> bool:
        # account id 以 "expired" 开头模拟登录失效
        return not account.id.startswith("expired")

    async def save_shared_files(
        self,
        account: Account,
        share_id: str,
        password: str | None,
        file_ids: list[str],
        destination: str | None,
        on_conflict: str,
    ) -> TransferResult:
        valid = await self.validate_account(account)
        if not valid:
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "account credential has expired",
                retryable=False,
            )
        if share_id.startswith("savefail"):
            raise DomainError(
                ErrorCode.QUOTA_EXCEEDED,
                "insufficient quota",
                retryable=False,
            )
        saved = [f"saved-{fid}" for fid in file_ids]
        return TransferResult(
            saved_file_ids=saved,
            remote_task_id=f"mock-remote-{share_id}",
        )

    async def create_folder(
        self, account: Account, name: str, parent_id: str = "0"
    ) -> str:
        return f"mock-folder-{name}"

    async def list_account_files(
        self, account: Account, parent_id: str | None = None
    ) -> list[ShareFile]:
        return self._sample_files("account")

    async def get_download_info(
        self, account: Account, file_id: str
    ) -> dict[str, Any]:
        if file_id.startswith("downloadfail"):
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                "download link expired",
                retryable=True,
            )
        return {
            "url": f"https://download.mock.local/{file_id}",
            "name": f"{file_id}.bin",
            "size": 1024,
            "checksum": None,
        }

    async def get_share_download_info(
        self,
        share_id: str,
        password: str | None,
        file_id: str,
        file: ShareFile | None = None,
    ) -> dict[str, Any]:
        """模拟匿名分享直链下载（无需账号）。"""
        if file_id.startswith("downloadfail"):
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                "download link expired",
                retryable=True,
            )
        name = file.name if file is not None else f"{file_id}.bin"
        return {
            "url": f"https://download.mock.local/{file_id}",
            "name": name,
            "size": file.size if file is not None else 1024,
            "checksum": None,
        }

    @staticmethod
    def _sample_files(prefix: str) -> list[ShareFile]:
        return [
            ShareFile(file_id=f"{prefix}-f1", name="video.mkv", size=1024),
            ShareFile(file_id=f"{prefix}-f2", name="subtitle.srt", size=64),
            ShareFile(file_id=f"{prefix}-d1", name="folder", is_dir=True),
        ]
