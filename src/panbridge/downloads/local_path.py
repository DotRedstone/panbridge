"""本地下载路径安全校验。

分享中的文件名/相对路径一律视为不可信输入，禁止其导致下载越界：
- 绝对路径
- `..` 目录穿越
- 空字节 / 非法分隔符
- 符号链接（中间目录或最终文件为 symlink 时拒绝，防止写穿到
  download_dir 之外）

这些校验同时服务于 Worker 的本地落盘路径解析与下载器的目标路径。
"""

from __future__ import annotations

import os
from pathlib import Path

from panbridge.domain.errors import DomainError, ErrorCode


def sanitize_rel_path(rel_path: str) -> str:
    """把分享给出的相对路径归一化为安全的下级相对路径。

    返回以 `/` 分隔、不含 `..`、不含绝对前缀的相对路径字符串。
    非法时抛出 INVALID_SHARE。
    """
    if not rel_path or "\x00" in rel_path:
        raise DomainError(
            ErrorCode.INVALID_SHARE,
            "invalid file path (empty or contains NUL)",
        )
    if rel_path.startswith(("/", "\\")) or (
        len(rel_path) >= 3 and rel_path[1] == ":" and rel_path[0].isalpha()
    ):
        raise DomainError(
            ErrorCode.INVALID_SHARE,
            "absolute path is not allowed in share file name",
        )

    # 统一分隔符为 `/`，逐段清洗
    parts = rel_path.replace("\\", "/").split("/")
    cleaned: list[str] = []
    for part in parts:
        p = part.strip()
        if p in ("", "."):
            continue
        if p == "..":
            raise DomainError(
                ErrorCode.INVALID_SHARE,
                "path traversal ('..') is not allowed in share file name",
            )
        cleaned.append(p)
    if not cleaned:
        raise DomainError(ErrorCode.INVALID_SHARE, "empty path after cleaning")
    return "/".join(cleaned)


def resolve_safe_local_path(download_dir: str, rel_path: str) -> str:
    """在 download_dir 下解析安全本地路径。

    - 先对 rel_path 做 sanitize_rel_path。
    - 用 os.path.realpath 归一化，确认最终路径仍位于 download_dir 内。
    - 若中间目录或最终路径是符号链接（指向外部），realpath 会暴露其
      真实位置，从而被「必须位于 download_dir 内」的断言拦截。

    非法时抛出 INVALID_SHARE。
    """
    safe_rel = sanitize_rel_path(rel_path)
    base = os.path.realpath(os.path.abspath(download_dir))
    # os.path.join 不会在 safe_rel 为绝对路径时越界（已被 sanitize 拒绝），
    # 但 realpath 仍用于消解 symlink。
    candidate = os.path.realpath(os.path.join(base, safe_rel))
    if candidate != base and not candidate.startswith(base + os.sep):
        raise DomainError(
            ErrorCode.INVALID_SHARE,
            "resolved download path escapes download directory",
        )
    return candidate


def ensure_no_symlink(path: str) -> None:
    """确保路径本身及其每个已存在的父目录都不是符号链接。

    用于下载前做防御性检查，避免已有 symlink 目录把写入重定向到外部。
    仅在路径已存在时校验；不存在的部分交给后续 makedirs 创建（普通目录）。
    """
    p = Path(path)
    # 检查已存在的最近祖先链
    cur: Path | None = p
    while cur is not None and cur != cur.parent:
        if cur.is_symlink():
            raise DomainError(
                ErrorCode.INVALID_SHARE,
                f"refusing to write through symlink: {cur}",
            )
        cur = cur.parent
    # 最终文件本身（若已存在）也禁止是 symlink
    if p.is_symlink():
        raise DomainError(
            ErrorCode.INVALID_SHARE,
            f"refusing to overwrite symlink: {p}",
        )
