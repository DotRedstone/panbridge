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


def test_extract_123pan_with_dash_password():
    result = extract_share("https://www.123pan.com/s/abc-1234")
    assert result.provider == "123"
    assert result.share_id == "abc"
    assert result.password == "1234"


def test_extract_aliyun_share():
    result = extract_share("https://www.alipan.com/s/abc123")
    assert result.provider == "aliyun"
    assert result.share_id == "abc123"


def test_extract_uc_share():
    result = extract_share("https://drive.uc.cn/s/abc")
    assert result.provider == "uc"
    assert result.share_id == "abc"


def test_extract_baidu_share_with_pwd_param():
    result = extract_share("https://pan.baidu.com/s/abc?pwd=1234")
    assert result.provider == "baidu"
    assert result.share_id == "abc"
    assert result.password == "1234"


def test_extract_115_share():
    result = extract_share("https://115.com/s/abc")
    assert result.provider == "115"
    assert result.share_id == "abc"


def test_extract_xunlei_share():
    result = extract_share("https://pan.xunlei.com/s/abc")
    assert result.provider == "xunlei"
    assert result.share_id == "abc"
