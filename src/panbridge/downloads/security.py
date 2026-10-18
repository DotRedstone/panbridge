"""SSRF 防护：URL 安全校验。

下载端点使用独立的安全策略，与分享域名白名单解耦：
下载 URL 必须来自可信 Provider 产生的下载信息，并在此做
二次校验——禁止访问内网、回环、云元数据地址，协议仅允许 http/https。

除对「字面 IP」的静态检查外，本模块还提供异步 DNS 解析校验
（`validate_download_url_async`），用于对域名解析后的真实 IP 做二次
拦截，防止「域名指向内网地址」的 SSRF 变体。重定向处理时对每一跳
都必须调用异步校验，避免跳转目标绕过字面 IP 检查。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

from panbridge.domain.errors import DomainError, ErrorCode

# 云元数据地址（常见）
_BLOCKED_HOSTS = {
    "metadata.google.internal",
    "169.254.169.254",
    "instance-data",
}

_BLOCKED_SUFFIXES = (
    ".local",
    ".internal",
    ".localhost",
)


def validate_download_url(url: str, *, allow_loopback: bool = False) -> str:
    """校验并规范化下载 URL（静态检查，不做 DNS 解析），返回 URL。

    不合规时抛出 DOWNLOAD_FAILED。

    `allow_loopback`：允许回环地址（127.0.0.0/8、::1）。仅用于本地
    集成测试（本地 HTTP 测试服务器）；生产环境务必保持 False。
    注意：即便开启，仍拒绝内网私有地址、云元数据地址、保留/多播地址。

    本函数只做「字面 IP + 主机名黑名单」检查；域名解析后的 IP 检查
    需调用 `validate_download_url_async`（涉及 IO，不能在纯同步函数里做）。
    """
    parsed = urlparse(url)
    _check_scheme_and_host(parsed, allow_loopback=allow_loopback)

    host_lower = parsed.hostname.lower()  # type: ignore[union-attr]
    # 解析为字面 IP 时，拒绝私有/回环/链路本地/保留地址
    try:
        ip = ipaddress.ip_address(host_lower)
    except ValueError:
        ip = None

    if ip is not None:
        if ip.is_loopback and allow_loopback:
            return url
        if _is_forbidden_ip(ip):
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                "download URL resolves to a forbidden address",
            )

    return url


async def validate_download_url_async(url: str, *, allow_loopback: bool = False) -> str:
    """校验下载 URL 并对其主机名做异步 DNS 解析检查，返回 URL。

    在 `validate_download_url` 的静态检查基础上，额外解析主机名的 A/AAAA
    记录，若解析出的任一 IP 为私有/回环/链路本地/保留/多播地址则拒绝。
    用于重定向等「域名可能被劫持指向内网」的场景。

    DNS 解析失败（无法解析 / 超时）时保守拒绝，避免「解析不了就放行」
    的安全漏洞。
    """
    parsed = urlparse(url)
    _check_scheme_and_host(parsed, allow_loopback=allow_loopback)
    host = parsed.hostname  # type: ignore[assignment]
    host_lower = host.lower()

    # 字面 IP 已在同步检查中处理；这里再走一遍确保一致
    try:
        ip = ipaddress.ip_address(host_lower)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.is_loopback and allow_loopback:
            return url
        if _is_forbidden_ip(ip):
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                "download URL resolves to a forbidden address",
            )
        return url

    # 域名：异步解析（放到线程池，避免阻塞事件循环）
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, None, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise DomainError(
            ErrorCode.DOWNLOAD_FAILED,
            f"download host failed to resolve: {host}",
        ) from exc

    for info in infos:
        addr = info[4][0]
        try:
            resolved_ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_forbidden_ip(resolved_ip):
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                "download URL resolves to a forbidden address",
            )

    return url


def _check_scheme_and_host(parsed, *, allow_loopback: bool) -> None:
    if parsed.scheme not in ("http", "https"):
        raise DomainError(
            ErrorCode.DOWNLOAD_FAILED,
            "download URL must be http(s)",
            details={"scheme": parsed.scheme},
        )
    host = parsed.hostname
    if not host:
        raise DomainError(ErrorCode.DOWNLOAD_FAILED, "download URL missing host")

    host_lower = host.lower()
    if host_lower in _BLOCKED_HOSTS or host_lower.endswith(_BLOCKED_SUFFIXES):
        raise DomainError(
            ErrorCode.DOWNLOAD_FAILED,
            "download host is forbidden",
        )


def _is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """返回该 IP 是否属于禁止访问的地址范围。

    回环地址在 `allow_loopback` 为 True 时由调用方单独放行，这里统一
    视为禁止（调用方已在上层处理放行逻辑）。
    """
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )
