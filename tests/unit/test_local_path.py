"""本地下载路径安全单元测试。"""

from __future__ import annotations

import os

import pytest

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.downloads.local_path import (
    ensure_no_symlink,
    resolve_safe_local_path,
    sanitize_rel_path,
)


def test_sanitize_keeps_relative_path():
    assert sanitize_rel_path("folder/deep.txt") == "folder/deep.txt"
    assert sanitize_rel_path("a/b/c.bin") == "a/b/c.bin"


def test_sanitize_normalizes_backslashes_and_dots():
    assert sanitize_rel_path("folder\\sub\\f.txt") == "folder/sub/f.txt"
    assert sanitize_rel_path("./a/./b") == "a/b"


def test_sanitize_rejects_parent_traversal():
    with pytest.raises(DomainError) as exc:
        sanitize_rel_path("../etc/passwd")
    assert exc.value.code == ErrorCode.INVALID_SHARE
    with pytest.raises(DomainError):
        sanitize_rel_path("a/../../b")


def test_sanitize_rejects_absolute_path():
    with pytest.raises(DomainError):
        sanitize_rel_path("/etc/passwd")
    with pytest.raises(DomainError):
        sanitize_rel_path("C:/windows/system32")
    with pytest.raises(DomainError):
        sanitize_rel_path("\\etc\\passwd")


def test_sanitize_rejects_nul_and_empty():
    with pytest.raises(DomainError):
        sanitize_rel_path("a\x00b")
    with pytest.raises(DomainError):
        sanitize_rel_path("")
    with pytest.raises(DomainError):
        sanitize_rel_path(".")


def test_resolve_safe_local_path_within_dir(tmp_path):
    dl = str(tmp_path / "dl")
    dest = resolve_safe_local_path(dl, "folder/deep.txt")
    assert dest == os.path.realpath(os.path.join(dl, "folder", "deep.txt"))


def test_resolve_safe_local_path_rejects_escape(tmp_path):
    dl = str(tmp_path / "dl")
    with pytest.raises(DomainError) as exc:
        resolve_safe_local_path(dl, "../outside.txt")
    assert exc.value.code == ErrorCode.INVALID_SHARE


def test_resolve_safe_local_path_rejects_symlink_escape(tmp_path):
    dl = tmp_path / "dl"
    dl.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    # 在 dl 内放一个指向外部的 symlink 目录
    (dl / "link").symlink_to(outside)
    with pytest.raises(DomainError):
        resolve_safe_local_path(str(dl), "link/evil.txt")


def test_ensure_no_symlink_rejects_symlink_file(tmp_path):
    target = tmp_path / "real.txt"
    target.write_text("x")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    with pytest.raises(DomainError):
        ensure_no_symlink(str(link))


def test_ensure_no_symlink_rejects_symlink_parent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    dl = tmp_path / "dl"
    dl.mkdir()
    (dl / "sub").symlink_to(outside)
    with pytest.raises(DomainError):
        ensure_no_symlink(str(dl / "sub" / "f.txt"))


def test_ensure_no_symlink_allows_normal(tmp_path):
    dl = tmp_path / "dl"
    dl.mkdir()
    ensure_no_symlink(str(dl / "not-yet-exists.txt"))
    (dl / "real.txt").write_text("x")
    ensure_no_symlink(str(dl / "real.txt"))
