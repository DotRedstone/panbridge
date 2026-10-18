"""扫码认证服务单元测试。

验证统一 `QrAuthService` 的会话编排逻辑（start/check/finish/cancel、
过期、取消、重复扫码、凭据加密入库），用 fake backend 模拟平台协议，
不访问真实网络（遵守「自动化测试不得操作真实网盘」原则）。

三个真实 backend（quark/aliyun/115）的协议模拟测试见
tests/unit/test_qr_backends.py。
"""

from __future__ import annotations

import pytest

from panbridge.application.auth import (
    QrAuthBackend,
    QrAuthService,
    QrCredential,
    QrStatus,
    QrTicket,
)
from panbridge.application.auth_service import AuthService
from panbridge.application.service import ApplicationService
from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.infrastructure.crypto import CredentialCipher
from panbridge.persistence import create_engine, create_sessionmaker, init_db
from panbridge.providers.quark.mock import MockProvider
from panbridge.providers.registry import ProviderRegistry


async def _async_true(account):
    return True


class _FakeBackend(QrAuthBackend):
    """可控的 fake 后端：模拟二维码与扫码状态流转。"""

    name = "fake"
    display_name = "Fake"

    def __init__(self):
        self.scanned = False
        self.confirmed = False
        self.cancelled_calls = 0

    async def start(self) -> QrTicket:
        return QrTicket(qr_url="https://fake.example/qr", state={})

    async def check(self, ticket: QrTicket) -> QrStatus:
        if self.confirmed:
            return QrStatus(status="success")
        if self.scanned:
            return QrStatus(status="scanned")
        return QrStatus(status="waiting")

    async def finish(self, ticket: QrTicket) -> QrCredential:
        if not self.confirmed:
            raise DomainError(ErrorCode.ACTION_REQUIRED, "not confirmed")
        return QrCredential(credential="fake-credential", label="fake-qr")

    async def cancel(self, ticket: QrTicket) -> None:
        self.cancelled_calls += 1


@pytest.fixture
def cipher():
    return CredentialCipher("test-secret-key-for-panbridge")


@pytest.fixture
async def svc(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await init_db(engine)
    sessionmaker = create_sessionmaker(engine)
    registry = ProviderRegistry()
    registry.register(MockProvider())
    service = ApplicationService(registry, sessionmaker)
    yield service, sessionmaker
    await engine.dispose()


@pytest.fixture
def fake_backend():
    return _FakeBackend()


@pytest.fixture
def qr_auth(svc, cipher, fake_backend):
    service, sessionmaker = svc
    qa = QrAuthService(sessionmaker, cipher)
    qa.register_backend(fake_backend)
    return qa


@pytest.mark.asyncio
async def test_start_login_returns_qr(qr_auth):
    info = await qr_auth.start_login("fake")
    assert info["provider"] == "fake"
    assert info["qr_url"].startswith("https://fake.example/")
    assert info["session_id"]


@pytest.mark.asyncio
async def test_unsupported_provider_raises(qr_auth):
    with pytest.raises(DomainError) as exc:
        await qr_auth.start_login("unknown")
    assert exc.value.code == ErrorCode.UNSUPPORTED_PROVIDER


@pytest.mark.asyncio
async def test_check_login_waiting_scanned_success(qr_auth, fake_backend):
    info = await qr_auth.start_login("fake")
    sid = info["session_id"]

    assert (await qr_auth.check_login(sid))["status"] == "waiting"

    fake_backend.scanned = True
    assert (await qr_auth.check_login(sid))["status"] == "scanned"

    fake_backend.confirmed = True
    assert (await qr_auth.check_login(sid))["status"] == "success"


@pytest.mark.asyncio
async def test_finish_persists_account(qr_auth, fake_backend, svc, cipher):
    service, sessionmaker = svc
    info = await qr_auth.start_login("fake")
    sid = info["session_id"]

    fake_backend.confirmed = True
    await qr_auth.check_login(sid)
    result = await qr_auth.finish_login(sid)

    assert result.logged_in is True
    assert result.account_id
    assert result.provider == "fake"

    account = await service.get_account(result.account_id)
    assert account.encrypted_credential
    assert "fake-credential" not in account.encrypted_credential
    assert cipher.decrypt(account.encrypted_credential) == "fake-credential"


@pytest.mark.asyncio
async def test_finish_before_confirm_raises(qr_auth):
    info = await qr_auth.start_login("fake")
    with pytest.raises(DomainError) as exc:
        await qr_auth.finish_login(info["session_id"])
    assert exc.value.code == ErrorCode.ACTION_REQUIRED


@pytest.mark.asyncio
async def test_check_expired_session(qr_auth):
    info = await qr_auth.start_login("fake")
    sid = info["session_id"]
    # 直接删除会话模拟过期，check 应返回 expired 而非抛错
    qr_auth._sessions.pop(sid, None)
    assert (await qr_auth.check_login(sid))["status"] == "expired"


@pytest.mark.asyncio
async def test_cancel_login(qr_auth, fake_backend):
    info = await qr_auth.start_login("fake")
    sid = info["session_id"]
    result = await qr_auth.cancel_login(sid)
    assert result["cancelled"] is True
    assert fake_backend.cancelled_calls == 1
    # 重复取消幂等
    result2 = await qr_auth.cancel_login(sid)
    assert result2["cancelled"] is False


@pytest.mark.asyncio
async def test_session_ttl_expiry(qr_auth):
    info = await qr_auth.start_login("fake")
    sid = info["session_id"]
    # 人为把会话时间拨回过去，模拟过期
    import time

    qr_auth._sessions[sid].created_at = time.time() - qr_auth.session_ttl - 1
    assert (await qr_auth.check_login(sid))["status"] == "expired"


@pytest.mark.asyncio
async def test_auth_service_gateway(svc, cipher, fake_backend):
    service, sessionmaker = svc
    qr_auth = QrAuthService(sessionmaker, cipher)
    qr_auth.register_backend(fake_backend)
    registry = ProviderRegistry()
    registry.register(MockProvider())
    auth = AuthService(service, registry, qr_auth)

    assert "fake" in auth.supported_qr_providers()

    info = await auth.start_qr_login("fake")
    fake_backend.confirmed = True
    await auth.check_qr_login(info["session_id"])
    result = await auth.finish_qr_login(info["session_id"])
    assert result["provider"] == "fake"
    assert result["logged_in"] is True
