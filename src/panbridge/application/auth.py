"""夸克扫码登录认证服务。

复用 QuarkPan（MIT）的 `APILogin` 底层协议能力
（`get_qr_code` / `check_login_status`），但**隔离其副作用**：

- 不用 `APILogin.login()` / `wait_for_login()`：它们内部有 `print`、
  倒计时线程、`time.sleep` 阻塞轮询，且会把 `login_result.json`、
  `user_info.json` 写到 QuarkPan 自己的配置目录（明文凭据副作用）。
- 不用 `QuarkAuth`：它会把 Cookie 明文写到 `cookies.json`。

本服务只复用「获取二维码」与「单次查询扫码状态」两个无副作用的协议
方法，其余（异步轮询、service_ticket 换 Cookie、凭据加密持久化）由
本模块自行实现，做到：

1. 不阻塞 HTTP 事件循环（轮询用 asyncio.sleep）。
2. 不重复保存明文凭据（Cookie 只在内存中短暂存在，最终加密进 DB）。
3. 登录结果直接写入 `AccountRepository`，接入现有账号体系。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.infrastructure.crypto import CredentialCipher
from panbridge.persistence import AccountRepository, session_scope

logger = logging.getLogger(__name__)

try:  # pragma: no cover - 依赖可选
    from quark_client.auth import APILogin
    from quark_client.exceptions import AuthenticationError as QuarkAuthError
except ImportError:  # pragma: no cover
    APILogin = None  # type: ignore
    QuarkAuthError = Exception  # type: ignore

# 扫码登录单次会话的默认有效期（秒）
_DEFAULT_SESSION_TTL = 300


@dataclass
class QrLoginSession:
    """一次扫码登录会话（内存态，不落盘）。"""

    session_id: str
    provider: str
    qr_token: str
    qr_url: str
    created_at: float = field(default_factory=time.time)
    # 内存中的认证会话持有 QuarkPan 的 APILogin 实例（含其 httpx client），
    # 用于轮询扫码状态与最终换 Cookie。
    login_mgr: Any = None


@dataclass
class QrLoginResult:
    """扫码登录完成后的结果。"""

    account_id: str
    provider: str
    label: str
    logged_in: bool = True


class QuarkQrAuthService:
    """夸克扫码登录用例服务。

    三阶段接口，同时服务 CLI 与 REST API：
    1. start_login()      —— 发起扫码，返回二维码（token + url）。
    2. check_login()      —— 单次轮询，返回是否已完成。
    3. finish_login()     —— 确认成功、换 Cookie、加密入库。
    """

    def __init__(
        self,
        sessionmaker,
        cipher: CredentialCipher,
        *,
        session_ttl: int = _DEFAULT_SESSION_TTL,
    ) -> None:
        if APILogin is None:  # pragma: no cover
            raise RuntimeError(
                "quarkpan 未安装，无法使用扫码登录。pip install quarkpan"
            )
        self.sessionmaker = sessionmaker
        self.cipher = cipher
        self.session_ttl = session_ttl
        # session_id -> QrLoginSession（内存，服务重启即失效）
        self._sessions: dict[str, QrLoginSession] = {}

    # ---- 会话管理 ----

    async def start_login(self, provider: str = "quark") -> dict[str, str]:
        """发起扫码登录，返回 {session_id, provider, qr_url}。

        qr_url 用于在终端渲染二维码（或交给前端/AstrBot 展示）。
        """
        if provider != "quark":
            raise DomainError(
                ErrorCode.UNSUPPORTED_PROVIDER,
                f"qr login not supported for provider: {provider}",
            )

        login_mgr = APILogin(timeout=self.session_ttl)
        # 复用 QuarkPan 获取二维码 token 与 URL 的协议实现
        try:
            qr_token, qr_url = await asyncio.to_thread(login_mgr.get_qr_code)
        except Exception as exc:  # noqa: BLE001
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"failed to obtain quark qr code: {exc.__class__.__name__}",
                retryable=True,
            ) from exc

        session_id = uuid.uuid4().hex
        session = QrLoginSession(
            session_id=session_id,
            provider=provider,
            qr_token=qr_token,
            qr_url=qr_url,
            login_mgr=login_mgr,
        )
        self._sessions[session_id] = session
        self._prune_expired()
        logger.info("qr login session %s started", session_id)
        return {
            "session_id": session_id,
            "provider": provider,
            "qr_url": qr_url,
        }

    async def check_login(self, session_id: str) -> dict[str, Any]:
        """单次查询扫码状态（非阻塞）。

        返回 {"status": "waiting"|"scanned"|"success"|"expired"|"failed"}。
        """
        session = self._get_session(session_id)
        if session is None:
            raise DomainError(
                ErrorCode.ACCOUNT_NOT_FOUND,
                f"qr login session not found or expired: {session_id}",
            )

        if time.time() - session.created_at > self.session_ttl:
            self._sessions.pop(session_id, None)
            return {"status": "expired"}

        # 复用 QuarkPan 的单次状态查询（无副作用）
        try:
            result = await asyncio.to_thread(
                session.login_mgr.check_login_status, session.qr_token
            )
        except Exception:  # noqa: BLE001
            return {"status": "waiting"}

        if result is None:
            # None 表示「仍在等待扫码」或「已扫码待确认」，无法区分，
            # 保守返回 waiting，由调用方继续轮询。
            return {"status": "waiting"}

        # 拿到 result 说明登录成功（check_login_status 仅成功时返回 dict）
        service_ticket = (
            result.get("data", {}).get("members", {}).get("service_ticket")
            if isinstance(result, dict)
            else None
        )
        if not service_ticket:
            return {"status": "waiting"}

        # 把 service_ticket 暂存到会话，供 finish_login 换 Cookie
        session.login_mgr._pb_service_ticket = service_ticket  # type: ignore[attr-defined]
        return {"status": "success"}

    async def finish_login(self, session_id: str) -> QrLoginResult:
        """确认登录成功：用 service_ticket 换 Cookie，加密入库。

        返回已创建/更新的账号信息。
        """
        session = self._get_session(session_id)
        if session is None:
            raise DomainError(
                ErrorCode.ACCOUNT_NOT_FOUND,
                f"qr login session not found or expired: {session_id}",
            )

        service_ticket = getattr(
            session.login_mgr, "_pb_service_ticket", None
        )
        if not service_ticket:
            # 尚未扫码成功
            raise DomainError(
                ErrorCode.ACTION_REQUIRED,
                "qr login not yet confirmed; scan the qr code first",
                retryable=False,
            )

        cookie = await self._exchange_cookie(service_ticket)
        if not cookie:
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "failed to exchange service ticket for quark cookie",
                retryable=False,
            )

        # 加密后持久化到账号库（复用现有账号体系）
        encrypted = self.cipher.encrypt(cookie)
        account_id = await self._upsert_account(encrypted)

        self._sessions.pop(session_id, None)
        logger.info("qr login session %s completed -> account %s",
                    session_id, account_id)
        return QrLoginResult(
            account_id=account_id,
            provider="quark",
            label="quark-qr",
        )

    # ---- 内部 ----

    async def _exchange_cookie(self, service_ticket: str) -> str:
        """用 service_ticket 换登录 Cookie（不复用 QuarkPan 的文件写入逻辑）。

        等价于 QuarkPan `_get_user_info_and_cookies` 的网络行为：
        请求 `https://pan.quark.cn/account/info?st=<ticket>&lw=scan`，
        响应的 Set-Cookie 会被写入独立 httpx client 的 cookie jar。
        这里不写任何文件，只提取 quark.cn 域名的 Cookie 拼成字符串。
        """
        async with httpx.AsyncClient(
            timeout=30.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/139.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
            },
        ) as client:
            resp = await client.get(
                "https://pan.quark.cn/account/info",
                params={"st": service_ticket, "lw": "scan"},
            )
            if resp.status_code != 200:
                logger.warning(
                    "exchange cookie got status %s", resp.status_code
                )
                return ""

            cookies: list[str] = []
            for cookie in client.cookies.jar:
                if cookie.domain and "quark.cn" in cookie.domain:
                    cookies.append(f"{cookie.name}={cookie.value}")
            return "; ".join(cookies)

    async def _upsert_account(self, encrypted_cookie: str) -> str:
        """把加密 Cookie 写入账号库（复用现有账号表）。"""
        from panbridge.domain.models import Account

        account_id = uuid.uuid4().hex
        async with session_scope(self.sessionmaker) as session:
            repo = AccountRepository(session)
            account = Account(
                id=account_id,
                provider="quark",
                label="quark-qr",
                encrypted_credential=encrypted_cookie,
            )
            await repo.add(account)
        return account_id

    def _get_session(self, session_id: str) -> QrLoginSession | None:
        return self._sessions.get(session_id)

    def _prune_expired(self) -> None:
        """清理过期会话（惰性触发，避免内存无限增长）。"""
        now = time.time()
        for sid in [
            s.session_id
            for s in self._sessions.values()
            if now - s.created_at > self.session_ttl
        ]:
            self._sessions.pop(sid, None)
