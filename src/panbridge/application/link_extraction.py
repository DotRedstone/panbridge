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
#
# 注意：此处登记「链接识别」所需的所有平台域名，即使对应 Adapter 尚未接入。
# 识别到平台但 registry 未注册时，下游会返回 UNSUPPORTED_PROVIDER（正确行为）。
_PLATFORM_PATTERNS: dict[str, tuple[set[str], Pattern[str]]] = {
    "quark": (
        {"pan.quark.cn"},
        re.compile(r"https?://pan\.quark\.cn/s/[A-Za-z0-9]+"),
    ),
    "123": (
        {
            "123pan.com",
            "www.123pan.com",
            "123684.com",
            "www.123684.com",
            "123685.com",
            "123912.com",
            "www.123912.com",
            "123pan.cn",
            "123592.com",
        },
        re.compile(
            r"https?://(?:www\.)?(?:123pan\.com|123684\.com|123685\.com|"
            r"123912\.com|123pan\.cn|123592\.com)/s/[A-Za-z0-9_-]+"
        ),
    ),
    "aliyun": (
        {"www.alipan.com", "alipan.com", "www.aliyundrive.com", "aliyundrive.com"},
        re.compile(
            r"https?://(?:www\.)?(?:alipan\.com|aliyundrive\.com)/s/[A-Za-z0-9_-]+"
        ),
    ),
    "uc": (
        {"drive.uc.cn"},
        re.compile(r"https?://drive\.uc\.cn/s/[A-Za-z0-9_-]+"),
    ),
    "baidu": (
        {"pan.baidu.com"},
        re.compile(r"https?://pan\.baidu\.com/s/[A-Za-z0-9_-]+"),
    ),
    "115": (
        {"115.com", "www.115.com", "115cdn.com", "anxia.com", "www.anxia.com"},
        re.compile(
            r"https?://(?:www\.)?(?:115\.com|115cdn\.com|anxia\.com)/s/[A-Za-z0-9_-]+"
        ),
    ),
    "xunlei": (
        {"pan.xunlei.com"},
        re.compile(r"https?://pan\.xunlei\.com/s/[A-Za-z0-9_-]+"),
    ),
    "mock": (
        {"mock.local", "pan.mock.cn"},
        re.compile(r"https?://(?:mock\.local|pan\.mock\.cn)/s/[A-Za-z0-9]+"),
    ),
}

# 提取码常见写法（第二项为密码所在捕获组索引）
_PASSWORD_LABEL = r"(?:提取码|提取碼|密码|密碼|验证码|验证碼|访问码|访问碼)"
_PASSWORD_PATTERNS: list[tuple[Pattern[str], int]] = [
    (re.compile(rf"{_PASSWORD_LABEL}\s*[:：]\s*([A-Za-z0-9]{{4,8}})"), 1),
    (re.compile(rf"{_PASSWORD_LABEL}\s*[是为]\s*([A-Za-z0-9]{{4,8}})"), 1),
    # 百度网盘：?pwd=xxxx 或 &pwd=xxxx
    (re.compile(r"[?&]pwd=([A-Za-z0-9]{4,8})"), 1),
    # 123 网盘：链接尾部 -xxxx 形式（/s/abc-1234）
    (re.compile(r"/([A-Za-z0-9]+)-([A-Za-z0-9]{4,8})$"), 2),
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
    password = _extract_password(text, url, provider)

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
        raw = parts[-1]
        # 123 网盘 share_id 尾部可能带 -密码 后缀，去掉它
        if "-" in raw:
            left, right = raw.rsplit("-", 1)
            if 4 <= len(right) <= 8 and left:
                return left
        return raw
    raise DomainError(ErrorCode.INVALID_SHARE, "malformed share URL")


def _extract_password(text: str, url: str, provider: str) -> str | None:
    """从文本中提取提取码。

    - 显式「提取码/密码：xxxx」形式优先。
    - 百度网盘 `?pwd=xxxx`。
    - 123 网盘 URL 尾部 `-xxxx`（share_id 与密码用连字符拼接）。
    """
    # 123 网盘：/s/<share_id>-<password> 形式，密码跟在连字符后
    if provider == "123":
        path = urlparse(url).path
        parts = [p for p in path.split("/") if p]
        if parts:
            raw = parts[-1]
            if "-" in raw:
                left, right = raw.rsplit("-", 1)
                if 4 <= len(right) <= 8 and left:
                    return right

    for pattern, group_idx in _PASSWORD_PATTERNS:
        m = pattern.search(text)
        if m:
            try:
                return m.group(group_idx)
            except IndexError:  # pragma: no cover
                continue
    return None
