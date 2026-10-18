"""内置 HTTP 流式下载器。

- 流式写入，不把整个文件装入内存
- 临时文件 + 原子重命名完成
- 超时控制
- 可选断点续传（远端支持 Range 且文件一致时）
- 安全的逐跳重定向：网盘直链普遍跳转至 CDN，禁用重定向会导致真实
  下载失败；因此手动逐跳跟随 3xx，但对每一跳重新做 URL/DNS/IP/SSRF
  校验，跨域时剥离 Cookie、Authorization 等敏感头，并限制跳数。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from urllib.parse import urljoin, urlparse

import httpx

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.downloads.base import Downloader, DownloadProgress
from panbridge.downloads.local_path import ensure_no_symlink
from panbridge.downloads.security import (
    validate_download_url,
    validate_download_url_async,
)

# 最大重定向跳数（超过即视为异常，拒绝继续跟随）
_DEFAULT_MAX_REDIRECTS = 5

# 跨域跳转时必须剥离的敏感请求头（避免把账号凭据泄漏给第三方 CDN）。
# 同域跳转保留全部头（如 Referer、Cookie 可能是 CDN 鉴权所需）。
_SENSITIVE_HEADERS = {
    "cookie",
    "authorization",
    "proxy-authorization",
    "x-auth-token",
    "x-api-key",
}


class HttpDownloader(Downloader):
    def __init__(
        self,
        *,
        timeout: float = 60.0,
        chunk_size: int = 64 * 1024,
        max_size: int | None = None,
        rate_limit: int | None = None,
        allow_loopback: bool = False,
        max_redirects: int = _DEFAULT_MAX_REDIRECTS,
    ) -> None:
        self._timeout = timeout
        self._chunk_size = chunk_size
        self._max_size = max_size
        # 下载限速（字节/秒），None 表示不限速。
        self._rate_limit = rate_limit
        # 允许回环地址下载（仅本地集成测试）。
        self._allow_loopback = allow_loopback
        self._max_redirects = max_redirects

    async def download(
        self,
        url: str,
        destination: str,
        *,
        headers: dict[str, str] | None = None,
        expected_size: int | None = None,
    ) -> AsyncIterator[DownloadProgress]:
        # 初始 URL 做静态校验（字面 IP/主机名黑名单，不做 DNS，避免
        # 单元测试 mock httpx 时触发真实 DNS）。重定向的每一跳在
        # _follow_redirects 里做含 DNS 解析的完整校验。
        validate_download_url(url, allow_loopback=self._allow_loopback)
        _guard_destination(destination)
        parent = os.path.dirname(destination) or "."
        os.makedirs(parent, exist_ok=True)

        tmp_path = destination + ".part"
        # 已写入的累计字节（含断点续传已存在的部分），用于限额判定。
        downloaded = 0
        # 是否已原子完成（.part 已 rename 为 destination）。finally 据此
        # 决定是否清理残留 .part。
        completed = False

        # 断点续传：若已有临时文件且远端支持 Range，从断点继续
        resume_from = 0
        if os.path.exists(tmp_path):
            resume_from = os.path.getsize(tmp_path)
            downloaded = resume_from

        req_headers = dict(headers or {})
        if resume_from > 0:
            req_headers["Range"] = f"bytes={resume_from}-"

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream(
                    "GET", url, headers=req_headers, follow_redirects=False
                ) as resp:
                    if resp.status_code == 416:  # Range 不满足，从头下载
                        resume_from = 0
                        downloaded = 0
                        req_headers.pop("Range", None)
                        await self._do_stream(
                            client,
                            url,
                            req_headers,
                            tmp_path,
                            expected_size,
                        )
                        return

                    # 3xx：安全的逐跳重定向处理
                    if _is_redirect_status(resp.status_code):
                        await self._follow_redirects(
                            client,
                            resp,
                            req_headers,
                            tmp_path,
                            expected_size,
                        )
                        # 重定向链处理完毕后，返回的生成器已把数据写盘。
                        # 这里重新累计 downloaded（重定向后的写入量）用于
                        # 下方原子完成与限额判定。
                        downloaded = os.path.getsize(tmp_path)
                    else:
                        resp.raise_for_status()
                        total = _parse_content_length(
                            resp.headers.get("content-length")
                        )
                        # 断点续传时 total 是全量大小，累加需还原绝对位置
                        if resume_from > 0 and total is not None:
                            total += resume_from
                        mode = "ab" if resume_from > 0 else "wb"
                        with open(tmp_path, mode) as f:
                            async for chunk in resp.aiter_bytes(self._chunk_size):
                                if (
                                    self._max_size
                                    and downloaded + len(chunk) > self._max_size
                                ):
                                    raise DomainError(
                                        ErrorCode.DOWNLOAD_FAILED,
                                        "download exceeds size limit",
                                    )
                                f.write(chunk)
                                downloaded += len(chunk)
                                # 限速：按 chunk 耗时控制在目标速率内
                                await self._throttle(len(chunk))
                                yield DownloadProgress(downloaded, total)

            # 原子完成
            os.replace(tmp_path, destination)
            completed = True
            yield DownloadProgress(downloaded, downloaded)
        except DomainError as exc:
            # 不可重试的失败（如大小超限）清理 .part；可重试的瞬时错误
            # 保留 .part 以支持断点续传。
            if not exc.retryable:
                _safe_remove(tmp_path)
            raise
        except httpx.HTTPError as exc:
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                f"http download failed: {exc.__class__.__name__}",
                retryable=True,
            ) from exc
        except OSError as exc:
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                f"download write failed: {exc.__class__.__name__}",
                retryable=True,
            ) from exc
        finally:
            # 下载被取消（GeneratorExit）或未正常完成时清理残留 .part，
            # 避免半成品文件污染 download_dir。成功完成后 completed 置真，
            # 且 .part 已被 rename，此处不删。断点续传（resume_from>0）时
            # 保留 .part 以支持下次续传。
            if not completed and resume_from == 0:
                _safe_remove(tmp_path)

    async def _follow_redirects(
        self,
        client: httpx.AsyncClient,
        first_resp: httpx.Response,
        headers: dict[str, str],
        tmp_path: str,
        expected_size: int | None,
    ) -> None:
        """安全的逐跳重定向处理：跟随 3xx 直到拿到非重定向响应或超限。

        从 `first_resp`（已确认为重定向响应）的 Location 开始逐跳跟随。
        每一跳：
        1. 用 urljoin 解析 Location（支持相对跳转）。
        2. 对该 URL 重新做 URL/DNS/IP/SSRF 校验。
        3. 跨域跳转时剥离 Cookie/Authorization 等敏感头。
        4. 跳数超过 `_max_redirects` 时拒绝（防重定向环/滥用）。

        拿到非重定向响应后流式写入 tmp_path（沿用断点续传的累计限额）。
        """
        # 第一跳：从 first_resp 的 Location 开始（first_resp 由外层
        # `async with client.stream(...)` 管理，此处不主动关闭，仅读取
        # Location；3xx 响应 body 为空，无需消费）。
        first_location = first_resp.headers.get("location")
        original_url = str(first_resp.url)

        if not first_location:
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                "redirect response missing Location header",
            )
        current_url = urljoin(original_url, first_location)
        # 首次跳转（原始 URL → 第一跳目标）也要做跨域敏感头剥离。
        current_headers = _strip_cross_origin_headers(
            original_url, current_url, dict(headers)
        )
        hops = 0

        while True:
            hops += 1
            if hops > self._max_redirects:
                raise DomainError(
                    ErrorCode.DOWNLOAD_FAILED,
                    f"too many redirects (>{self._max_redirects})",
                )

            # 每一跳发起前重新做 SSRF + DNS 校验
            await validate_download_url_async(
                current_url, allow_loopback=self._allow_loopback
            )

            # 跨域跳转：用独立客户端发起，避免把主 client 的 cookie jar
            # （含账号 Cookie）带到第三方 CDN。独立 client 的 cookie jar
            # 为空，配合 current_headers 已剥离的敏感头，双重保障凭据
            # 不跨域泄漏。同域跳转复用主 client 以保留连接复用。
            req_client: httpx.AsyncClient
            if _same_origin(original_url, current_url):
                req_client = client
                close_req_client = False
            else:
                req_client = httpx.AsyncClient(timeout=self._timeout)
                close_req_client = True

            try:
                async with req_client.stream(
                    "GET", current_url, headers=current_headers
                ) as resp:
                    if _is_redirect_status(resp.status_code):
                        # 仍是重定向：解析下一跳
                        location = resp.headers.get("location")
                        if not location:
                            raise DomainError(
                                ErrorCode.DOWNLOAD_FAILED,
                                "redirect response missing Location header",
                            )
                        next_url = urljoin(current_url, location)
                        current_headers = _strip_cross_origin_headers(
                            current_url, next_url, current_headers
                        )
                        current_url = next_url
                        continue  # 进入下一跳

                    # 拿到最终响应：流式写入
                    resp.raise_for_status()
                    downloaded = (
                        os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
                    )
                    mode = "ab" if downloaded > 0 else "wb"
                    with open(tmp_path, mode) as f:
                        async for chunk in resp.aiter_bytes(self._chunk_size):
                            if (
                                self._max_size
                                and downloaded + len(chunk) > self._max_size
                            ):
                                raise DomainError(
                                    ErrorCode.DOWNLOAD_FAILED,
                                    "download exceeds size limit",
                                )
                            f.write(chunk)
                            downloaded += len(chunk)
                            await self._throttle(len(chunk))
                    break
            finally:
                if close_req_client:
                    await req_client.aclose()

    async def _throttle(self, chunk_len: int) -> None:
        """按全局速率限制节流（简单 token-bucket 近似）。"""
        if not self._rate_limit or self._rate_limit <= 0:
            return
        # 每 chunk 应耗时 = chunk_len / rate_limit 秒
        wait = chunk_len / self._rate_limit
        await asyncio.sleep(wait)

    async def _do_stream(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        tmp_path: str,
        expected_size: int | None,
    ) -> None:
        """从头重新流式写入（用于 Range 416 回退）。"""
        downloaded = 0
        async with client.stream(
            "GET", url, headers=headers, follow_redirects=False
        ) as resp:
            resp.raise_for_status()
            with open(tmp_path, "wb") as f:
                async for chunk in resp.aiter_bytes(self._chunk_size):
                    if self._max_size and downloaded + len(chunk) > self._max_size:
                        raise DomainError(
                            ErrorCode.DOWNLOAD_FAILED,
                            "download exceeds size limit",
                        )
                    f.write(chunk)
                    downloaded += len(chunk)


def _is_redirect_status(status_code: int) -> bool:
    """判断 HTTP 状态码是否为重定向（3xx，含 301/302/303/307/308）。"""
    return status_code in (301, 302, 303, 307, 308)


def _same_origin(url_a: str, url_b: str) -> bool:
    """判断两个 URL 是否同源（scheme + host + 端口一致）。"""
    a = urlparse(url_a)
    b = urlparse(url_b)
    return (
        a.scheme == b.scheme
        and (a.hostname or "").lower() == (b.hostname or "").lower()
        and _effective_port(a) == _effective_port(b)
    )


def _strip_cross_origin_headers(
    current_url: str, next_url: str, headers: dict[str, str]
) -> dict[str, str]:
    """跨域跳转时剥离敏感请求头；同域跳转保留全部。

    判断「跨域」依据 scheme + host（含端口）；路径不同视为同域。
    """
    if _same_origin(current_url, next_url):
        return headers
    return {k: v for k, v in headers.items() if k.lower() not in _SENSITIVE_HEADERS}


def _effective_port(parsed) -> int:
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def _parse_content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _guard_destination(destination: str) -> None:
    """下载前防御性检查目标路径，拒绝符号链接写入。

    注意：Worker 已在 `_resolve_local_path` 做了 sanitize + realpath 越界
    校验；此处再补一层 symlink 检查，作为下载器独立的安全底线，
    避免被其他调用方（绕过 Worker）直接使用时写穿到外部。
    """
    ensure_no_symlink(destination)


def _safe_remove(path: str) -> None:
    """尽力删除文件/残留 .part，失败不抛错。"""
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
