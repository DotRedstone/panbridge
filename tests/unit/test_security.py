"""SSRF 防护单元测试。"""

from __future__ import annotations

import pytest

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.downloads.security import validate_download_url


def test_valid_https_url():
    assert validate_download_url("https://download.example.com/f.bin") == (
        "https://download.example.com/f.bin"
    )


def test_reject_non_http_scheme():
    with pytest.raises(DomainError) as exc:
        validate_download_url("ftp://example.com/f.bin")
    assert exc.value.code == ErrorCode.DOWNLOAD_FAILED


def test_reject_loopback():
    with pytest.raises(DomainError) as exc:
        validate_download_url("http://127.0.0.1/admin")
    assert exc.value.code == ErrorCode.DOWNLOAD_FAILED


def test_reject_private_ip():
    with pytest.raises(DomainError) as exc:
        validate_download_url("http://192.168.1.1/secret")
    assert exc.value.code == ErrorCode.DOWNLOAD_FAILED


def test_reject_metadata_host():
    with pytest.raises(DomainError) as exc:
        validate_download_url("http://169.254.169.254/latest/meta-data")
    assert exc.value.code == ErrorCode.DOWNLOAD_FAILED


def test_reject_internal_suffix():
    with pytest.raises(DomainError) as exc:
        validate_download_url("http://foo.internal/x")
    assert exc.value.code == ErrorCode.DOWNLOAD_FAILED
