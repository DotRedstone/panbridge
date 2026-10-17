"""分享链接提取与平台识别。

从不可信的聊天文本中提取网盘分享链接与提取码，
并识别平台。所有输入均视为不可信。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from re import Pattern
from urllib.parse import urlparse

from panbridge.domain.errors import DomainError, ErrorCode

# 平台 -> (域名集合, 链接正则)
_PLATFORM_PATTERNS: dict[str, tuple[set[str], Pattern[str]]] = {
    "quark": (
        {"pan.quark.cn"},
        re.compile(r"https?://pan\.quark\.cn/s/[A-Za-z0-9]+"),
    ),
    "mock": (
        {"mock.local", "pan.mock.cn"},
        re.compile(r"https?://(?:mock\.local|pan\.mock\.cn)/s/[A-Za-z0-9]+"),
    ),
}

# 提取码常见写法
_PASSWORD_LABEL = r"(?:提取码|提取碼|密码|密碼|验证码|验证碼|访问码|访问碼)"
_PASSWORD_PATTERNS: list[tuple[Pattern[str], int]] = [
    (re.compile(rf"{_PASSWORD_LABEL}\s*[:：]\s*([A-Za-z0-9]{{4,8}})"), 0),
    (re.compile(rf"{_PASSWORD_LABEL}\s*[是为]\s*([A-Za-z0-9]{{4,8}})"), 0),
]


@dataclass(frozen=True)
class ExtractedShare:
    provider: str
    url: str
    share_id: str
    password: str | None


def extract_share(text: str) -> ExtractedShare:
    """从文本中提取第一个可识别的分享链接。

    若文本包含多个平台链接，返回第一个匹配项。
    无法识别时抛出 INVALID_SHARE。
    """
    if not text or not text.strip():
        raise DomainError(ErrorCode.INVALID_SHARE, "empty input text")

    matches: list[tuple[int, str, str]] = []  # (position, provider, url)
    for provider, (_, pattern) in _PLATFORM_PATTERNS.items():
        for m in pattern.finditer(text):
            matches.append((m.start(), provider, m.group(0)))

    if not matches:
        raise DomainError(
            ErrorCode.INVALID_SHARE,
            "no supported share link found in text",
        )

    matches.sort(key=lambda x: x[0])
    _, provider, url = matches[0]
    share_id = _share_id_from_url(url)
    password = _extract_password(text)

    return ExtractedShare(
        provider=provider,
        url=url,
        share_id=share_id,
        password=password,
    )


def identify_provider(url: str) -> str:
    host = urlparse(url).hostname or ""
    for provider, (domains, _) in _PLATFORM_PATTERNS.items():
        if host in domains:
            return provider
    raise DomainError(
        ErrorCode.UNSUPPORTED_PROVIDER,
        f"unsupported share host: {host}",
        details={"host": host},
    )


def _share_id_from_url(url: str) -> str:
    path = urlparse(url).path
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[-2] == "s":
        return parts[-1]
    raise DomainError(ErrorCode.INVALID_SHARE, "malformed share URL")


def _extract_password(text: str) -> str | None:
    for pattern, _ in _PASSWORD_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(1)
    return None
