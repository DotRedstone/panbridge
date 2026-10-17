"""链接提取单元测试。"""

from __future__ import annotations

import pytest

from panbridge.application.link_extraction import extract_share
from panbridge.domain.errors import DomainError, ErrorCode


def test_extract_quark_share_with_password():
    text = "https://pan.quark.cn/s/abc123 提取码: 1234"
    result = extract_share(text)
    assert result.provider == "quark"
    assert result.share_id == "abc123"
    assert result.password == "1234"


def test_extract_quark_share_without_password():
    text = "看这个 https://pan.quark.cn/s/xyz789"
    result = extract_share(text)
    assert result.provider == "quark"
    assert result.share_id == "xyz789"
    assert result.password is None


def test_extract_password_colon_style():
    text = "https://pan.quark.cn/s/abc 密码：8888"
    result = extract_share(text)
    assert result.password == "8888"


def test_extract_mock_share():
    text = "https://mock.local/s/mock001"
    result = extract_share(text)
    assert result.provider == "mock"
    assert result.share_id == "mock001"


def test_no_share_raises_invalid_share():
    with pytest.raises(DomainError) as exc:
        extract_share("这里没有任何链接")
    assert exc.value.code == ErrorCode.INVALID_SHARE


def test_empty_text_raises():
    with pytest.raises(DomainError) as exc:
        extract_share("")
    assert exc.value.code == ErrorCode.INVALID_SHARE
