"""下载层：统一下载接口。

第一版提供内置 HTTP 流式下载；Aria2 作为后续可选后端。
下载管理器不依赖任何具体网盘。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass
class DownloadProgress:
    downloaded: int
    total: int | None


class Downloader(ABC):
    @abstractmethod
    async def download(
        self,
        url: str,
        destination: str,
        *,
        headers: dict[str, str] | None = None,
        expected_size: int | None = None,
    ) -> AsyncIterator[DownloadProgress]:
        """流式下载到 destination，yield 进度。

        实现应负责：临时文件 + 原子完成、超时、断点续传（可选）、
        磁盘与大小校验。
        """
        raise NotImplementedError
