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
            r"|https?://[A-Za-z0-9]+\.share\.123pan\.cn/[A-Za-z0-9]+/[A-Za-z0-9_-]+"
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
    "weiyun": (
        {"share.weiyun.com", "www.weiyun.com"},
        re.compile(r"https?://(?:www\.)?(?:share\.)?weiyun\.com/[A-Za-z0-9]+"),
    ),
    "cloud189": (
        {"cloud.189.cn", "www.cloud.189.cn"},
        # 天翼云盘分享：https://cloud.189.cn/t/<code> 或 /web/share?code=<code>
        re.compile(
            r"https?://(?:www\.)?cloud\.189\.cn/(?:t/[A-Za-z0-9_-]+"
            r"|web/share\?code=[A-Za-z0-9_-]+)"
        ),
    ),
    "caiyun": (
        {"yun.139.com", "caiyun.139.com", "www.caiyun.139.com"},
        # 移动云盘分享链接：https://yun.139.com/shareweb/#/w/i/<linkID>
        re.compile(
            r"https?://(?:www\.)?(?:yun|caiyun)\.139\.com/shareweb/?"
            r"(?:#/)?w/i/[A-Za-z0-9]+"
        ),
    ),
    "lanzou": (
        {
            "lanzou.com",
            "www.lanzou.com",
            "lanzoui.com",
            "www.lanzoui.com",
            "lanzoux.com",
            "lanzouw.com",
            "lanzouy.com",
            "lanzoub.com",
            "lanzous.com",
            "lanzouu.com",
            "ilanzou.com",
            "www.ilanzou.com",
        },
        # 蓝奏云短链路径为单段 key（如 /abc123 或 /iAbcDef），
        # 非 /s/<id> 形式，故单独匹配。
        # 蓝奏云域名多变：前缀可为任意子域名（如 wwawu），后缀为
        # lanzou 后接 1-2 个字母（lanzou/i/x/w/y/b/s/u/j 等）。
        re.compile(
            r"https?://(?:[a-z0-9]+\.)?lanzou[a-z]{0,2}\.com/[A-Za-z0-9]{6,}"
        ),
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
    # 紧凑写法：密码标签后直接跟提取码，无冒号（如「密码eke0」或「密码 eke0」）。
    (re.compile(rf"{_PASSWORD_LABEL}[ \t]*([A-Za-z0-9]{{4,8}})"), 1),
    # 百度网盘：?pwd=xxxx 或 &pwd=xxxx
    (re.compile(r"[?&]pwd=([A-Za-z0-9]{4,8})"), 1),
    # 123 网盘：链接尾部 -xxxx 形式（/s/abc-1234），提取码为 1-4 位。
    # 超过 4 位（如 A6cA-I24Jh 的 I24Jh）是分享定位符，非提取码。
    (re.compile(r"/([A-Za-z0-9]+)-([A-Za-z0-9]{1,4})$"), 2),
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
    # 蓝奏云：域名众多（lanzou/i/x/w/y/b/s 等子域名），且路径为单段 key，
    # 直接把完整 URL 作为 share_id 传给 Provider，由它归一化。
    if "lanzou" in url or "ilanzou" in url:
        return url

    # 移动云盘：/shareweb/#/w/i/<linkID>，提取 linkID
    if "139.com/shareweb" in url:
        m = re.search(r"/w/i/([A-Za-z0-9]+)", url)
        if m:
            return m.group(1)
        raise DomainError(ErrorCode.INVALID_SHARE, "malformed caiyun share URL")

    # 微云：share.weiyun.com/<key>，路径为单段 key
    if "weiyun.com" in url:
        path = urlparse(url).path
        parts = [p for p in path.split("/") if p]
        if parts:
            return parts[-1]
        raise DomainError(ErrorCode.INVALID_SHARE, "malformed weiyun share URL")

    # 天翼云盘：cloud.189.cn/t/<code> 或 /web/share?code=<code>
    if "189.cn" in url:
        m = re.search(r"/t/([A-Za-z0-9_-]+)", url)
        if m:
            return m.group(1)
        m = re.search(r"code=([A-Za-z0-9_-]+)", url)
        if m:
            return m.group(1)
        raise DomainError(ErrorCode.INVALID_SHARE, "malformed cloud189 share URL")

    # 123 网盘个性化域名分享：<uid>.share.123pan.cn/<prefix>/<shareKey>
    # 形如 https://1822747000.share.123pan.cn/123pan/k73Mjv-3vrnA，
    # share_id 为路径最后一段（完整短码，连字符是短码一部分）。
    if ".share.123pan.cn" in url:
        path = urlparse(url).path
        parts = [p for p in path.split("/") if p]
        if parts:
            return parts[-1]
        raise DomainError(ErrorCode.INVALID_SHARE, "malformed 123 share URL")

    path = urlparse(url).path
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[-2] in ("s", "share"):
        raw = parts[-1]
        # 123 网盘 share_id 保留完整短码（如 A6cA-I24Jh），
        # 连字符后可能是提取码（≤4 位）或分享定位符，不在此拆分，
        # 由 Provider 的短码解析接口（/gsb/s/share-key）统一处理。
        return raw
    raise DomainError(ErrorCode.INVALID_SHARE, "malformed share URL")


def _extract_password(text: str, url: str, provider: str) -> str | None:
    """从文本中提取提取码。

    - 显式「提取码/密码：xxxx」形式优先。
    - 百度网盘 `?pwd=xxxx`。
    - 123 网盘 URL 尾部 `-xxxx`（share_id 与密码用连字符拼接）。
    """
    # 123 网盘：优先从 `?pwd=` 参数取提取码（个性化域名新格式明确给出）。
    # 只有旧格式 `/s/<share_id>-<password>` 才从 URL 尾部连字符拆分，
    # 且连字符后 1-4 位才是提取码；超过 4 位（如 A6cA-I24Jh 的 I24Jh）
    # 是分享定位符，不是提取码。
    if provider == "123":
        pwd_match = re.search(r"[?&]pwd=([A-Za-z0-9]{1,8})", url)
        if pwd_match:
            return pwd_match.group(1)
        path = urlparse(url).path
        parts = [p for p in path.split("/") if p]
        if parts and "-" in parts[-1]:
            raw = parts[-1]
            left, right = raw.rsplit("-", 1)
            if 1 <= len(right) <= 4 and left:
                return right

    for pattern, group_idx in _PASSWORD_PATTERNS:
        m = pattern.search(text)
        if m:
            try:
                return m.group(group_idx)
            except IndexError:  # pragma: no cover
                continue
    return None
