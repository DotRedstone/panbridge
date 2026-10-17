"""扫码认证服务单元测试。

通过 mock quark_client.auth.APILogin 验证扫码登录流程的编排逻辑，
不访问真实网络（遵守「自动化测试不得操作真实网盘」原则）。
"""

from __future__ import annotations

import pytest

from panbridge.application.auth import QuarkQrAuthService
from panbridge.application.auth_service import AuthService
from panbridge.application.service import ApplicationService
from panbridge.domain.errors import DomainError
from panbridge.infrastructure.crypto import CredentialCipher
from panbridge.persistence import create_engine, create_sessionmaker, init_db
from panbridge.providers.quark.mock import MockProvider
from panbridge.providers.registry import ProviderRegistry


async def _async_true(account):
    return True


class _FakeAPILogin:
    """模拟 QuarkPan APILogin：可控的二维码与登录状态。"""

    def __init__(self, timeout=300):
        self.timeout = timeout
        self._pb_service_ticket = None

    def get_qr_code(self):
        return "fake-qr-token", "https://su.quark.cn/fake?token=fake"

    def check_login_status(self, qr_token):
        # 扫码前返回 None，扫码后返回含 service_ticket 的 dict
        if self._pb_service_ticket:
            return {
                "status": 2000000,
                "message": "ok",
                "data": {
                    "members": {
                        "service_ticket": self._pb_service_ticket,
                    }
                },
            }
        return None


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
def fake_login(monkeypatch):
    fake = _FakeAPILogin()
    monkeypatch.setattr(
        "panbridge.application.auth.APILogin", lambda **kw: fake
    )
    return fake


@pytest.mark.asyncio
async def test_start_login_returns_qr(svc, cipher, fake_login):
    service, sessionmaker = svc
    qr_auth = QuarkQrAuthService(sessionmaker, cipher)
    info = await qr_auth.start_login("quark")
    assert info["provider"] == "quark"
    assert info["qr_url"].startswith("https://su.quark.cn/")
    assert info["session_id"]


@pytest.mark.asyncio
async def test_check_login_waiting_then_success(svc, cipher, fake_login):
    service, sessionmaker = svc
    qr_auth = QuarkQrAuthService(sessionmaker, cipher)
    info = await qr_auth.start_login("quark")
    sid = info["session_id"]

    # 尚未扫码 → waiting
    status = await qr_auth.check_login(sid)
    assert status["status"] == "waiting"

    # 模拟扫码成功
    fake_login._pb_service_ticket = "fake-service-ticket"
    status = await qr_auth.check_login(sid)
    assert status["status"] == "success"


@pytest.mark.asyncio
async def test_finish_login_persists_account(svc, cipher, fake_login, monkeypatch):
    service, sessionmaker = svc
    qr_auth = QuarkQrAuthService(sessionmaker, cipher)

    # 拦截换 Cookie 的网络请求
    async def _fake_exchange(ticket):
        return "__kps=abc; __uid=def"

    monkeypatch.setattr(qr_auth, "_exchange_cookie", _fake_exchange)

    info = await qr_auth.start_login("quark")
    sid = info["session_id"]
    fake_login._pb_service_ticket = "fake-service-ticket"
    await qr_auth.check_login(sid)
    result = await qr_auth.finish_login(sid)

    assert result.logged_in is True
    assert result.account_id

    # 账号已加密入库，且解密后不含明文 Cookie
    account = await service.get_account(result.account_id)
    assert account.encrypted_credential
    assert "abc" not in account.encrypted_credential
    # 解密后还原 Cookie
    assert cipher.decrypt(account.encrypted_credential) == "__kps=abc; __uid=def"


@pytest.mark.asyncio
async def test_finish_before_scan_raises(svc, cipher, fake_login):
    service, sessionmaker = svc
    qr_auth = QuarkQrAuthService(sessionmaker, cipher)
    info = await qr_auth.start_login("quark")
    with pytest.raises(DomainError):
        await qr_auth.finish_login(info["session_id"])


@pytest.mark.asyncio
async def test_login_status_and_logout(svc, cipher, fake_login, monkeypatch):
    service, sessionmaker = svc
    qr_auth = QuarkQrAuthService(sessionmaker, cipher)
    registry = ProviderRegistry()
    # 注册真实 QuarkProvider，但拦截 validate_account 避免真实网络
    from panbridge.providers.quark.provider import QuarkProvider

    qp = QuarkProvider(cipher=cipher)
    monkeypatch.setattr(qp, "validate_account", _async_true)
    registry.register(qp)
    auth = AuthService(service, registry, qr_auth)

    # 未登录
    status = await auth.login_status("quark")
    assert status["logged_in"] is False

    # 登录后（用 mock provider 的账号，绕开真实校验）
    from panbridge.domain.models import Account
    from panbridge.persistence import AccountRepository, session_scope

    async with session_scope(sessionmaker) as session:
        account = Account(
            id="acc-quark",
            provider="quark",
            label="test",
            encrypted_credential=cipher.encrypt("cookie"),
        )
        await AccountRepository(session).add(account)

    # MockProvider.validate_account 对 quark 账号默认返回 True（非 expired）
    status = await auth.login_status("quark")
    assert status["logged_in"] is True

    # 退出登录
    result = await auth.logout("quark")
    assert result["removed"] >= 1
    status = await auth.login_status("quark")
    assert status["logged_in"] is False
