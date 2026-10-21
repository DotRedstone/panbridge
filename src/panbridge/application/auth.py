"""统一扫码登录认证服务。

把「扫码登录」从夸克专用泛化为多平台：抽象出 `QrAuthBackend` 协议，
每个平台（夸克 / 阿里云盘 / 115 网盘）实现「获取二维码 → 轮询状态 →
确认完成 → 取消」四阶段接口，统一由 `QrAuthService` 编排会话生命周期，
并对接 `AccountRepository` 加密持久化凭据。

设计目标：
1. 不把业务逻辑硬编码为 Quark 专用；新增平台只需实现一个 backend。
2. 会话统一支持：过期、取消、重复扫码、凭据失效后的重新登录。
3. 凭据只在内存中短暂存在，最终加密进 DB；不把 SDK 的明文凭据
   （aligo 的 ~/.aligo、p115client 的 cookie 文件、QuarkPan 的
   cookies.json）长期留在项目外部目录。
4. 不阻塞 HTTP 事件循环（轮询用 asyncio.sleep，SDK 调用走 to_thread）。

Backend 协议（`QrAuthBackend`）：
- `start() -> QrTicket`：发起扫码，返回二维码内容/URL 与轮询所需的
  不透明 state（供后续 check/finish 使用）。
- `check(ticket, state) -> QrStatus`：单次轮询，返回 waiting/scanned/
  success/expired/cancelled/failed。
- `finish(state) -> QrCredential`：确认登录成功，换取最终凭据
  （Cookie / refresh_token 字符串），供加密入库。
- `cancel(state)`：主动取消（部分平台可调远端取消接口，否则仅丢弃
  本地 state）。
"""

from __future__ import annotations

import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.infrastructure.crypto import CredentialCipher
from panbridge.persistence import AccountRepository, session_scope

logger = logging.getLogger(__name__)

# 扫码登录单次会话的默认有效期（秒）
_DEFAULT_SESSION_TTL = 300


@dataclass
class QrTicket:
    """发起扫码后的结果：二维码内容 + 轮询所需的不透明 state。"""

    qr_url: str
    # 平台后端轮询/确认所需的私有 state（如夸克的 token、阿里云盘的
    # data、115 的 payload）。只在内存中，不落盘。
    state: Any = None


@dataclass
class QrStatus:
    """单次轮询结果。"""

    status: str  # waiting | scanned | success | expired | cancelled | failed
    # 成功后暂存的中间凭据（如夸克 service_ticket），供 finish 使用。
    intermediate: Any = None


@dataclass
class QrCredential:
    """扫码登录最终凭据（将加密入库）。"""

    # 待加密的明文凭据字符串（Cookie 或 refresh_token 等）。
    credential: str
    label: str


class QrAuthBackend(ABC):
    """单平台扫码登录后端协议。

    实现类应为无状态（或仅持有只读配置），所有会话状态通过
    `ticket.state` 在 start/check/finish 之间传递。禁止把明文凭据写入
    任何持久化位置（文件、SDK 默认目录）。
    """

    name: str
    display_name: str

    @abstractmethod
    async def start(self) -> QrTicket:
        """发起扫码，返回二维码与轮询 state。"""

    @abstractmethod
    async def check(self, ticket: QrTicket) -> QrStatus:
        """单次轮询扫码状态（非阻塞，失败/超时返回 waiting 而非抛错）。"""

    @abstractmethod
    async def finish(self, ticket: QrTicket) -> QrCredential:
        """确认登录成功，换取最终凭据。未成功时抛 ACTION_REQUIRED。"""

    async def cancel(self, ticket: QrTicket) -> None:  # noqa: BLE001
        """主动取消会话。默认仅丢弃本地 state；平台若支持远端取消可覆盖。"""
        return None


@dataclass
class QrLoginSession:
    """一次扫码登录会话（内存态，不落盘）。"""

    session_id: str
    provider: str
    ticket: QrTicket
    backend: QrAuthBackend
    created_at: float = field(default_factory=time.time)
    # 已确认成功、可 finish 的标志（check 返回 success 后置真）。
    confirmed: bool = False


@dataclass
class QrLoginResult:
    """扫码登录完成后的结果。"""

    account_id: str
    provider: str
    label: str
    logged_in: bool = True


class QrAuthService:
    """多平台扫码登录用例服务。

    三阶段接口，同时服务 CLI 与 REST API：
    1. start_login()      —— 发起扫码，返回二维码（token + url）。
    2. check_login()      —— 单次轮询，返回是否已完成。
    3. finish_login()     —— 确认成功、换凭据、加密入库。
    4. cancel_login()     —— 主动取消会话。

    会话统一处理过期、取消、重复扫码（check 幂等，finish 一次性）。
    """

    def __init__(
        self,
        sessionmaker,
        cipher: CredentialCipher,
        backends: dict[str, QrAuthBackend] | None = None,
        *,
        session_ttl: int = _DEFAULT_SESSION_TTL,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.cipher = cipher
        self.session_ttl = session_ttl
        self._backends: dict[str, QrAuthBackend] = dict(backends or {})
        # session_id -> QrLoginSession（内存，服务重启即失效）
        self._sessions: dict[str, QrLoginSession] = {}

    def register_backend(self, backend: QrAuthBackend) -> None:
        """注册一个平台的扫码后端。"""
        self._backends[backend.name] = backend

    def supported_providers(self) -> list[str]:
        return sorted(self._backends.keys())

    def _backend_for(self, provider: str) -> QrAuthBackend:
        backend = self._backends.get(provider)
        if backend is None:
            raise DomainError(
                ErrorCode.UNSUPPORTED_PROVIDER,
                f"qr login not supported for provider: {provider}",
            )
        return backend

    # ---- 会话管理 ----

    async def start_login(self, provider: str = "quark") -> dict[str, str]:
        """发起扫码登录，返回 {session_id, provider, qr_url}。"""
        backend = self._backend_for(provider)
        self._prune_expired()
        try:
            ticket = await backend.start()
        except DomainError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"failed to obtain {provider} qr code: "
                f"{exc.__class__.__name__}",
                retryable=True,
            ) from exc

        session_id = uuid.uuid4().hex
        self._sessions[session_id] = QrLoginSession(
            session_id=session_id,
            provider=provider,
            ticket=ticket,
            backend=backend,
        )
        logger.info("qr login session %s started for %s", session_id, provider)
        return {
            "session_id": session_id,
            "provider": provider,
            "display_name": getattr(backend, "display_name", provider),
            "qr_url": ticket.qr_url,
        }

    async def check_login(self, session_id: str) -> dict[str, Any]:
        """单次查询扫码状态（非阻塞）。

        返回 {"status": "waiting"|"scanned"|"success"|"expired"|
        "cancelled"|"failed"}。
        """
        session = self._sessions.get(session_id)
        if session is None:
            # 会话不存在（已过期被清理或从未创建）
            return {"status": "expired"}

        if time.time() - session.created_at > self.session_ttl:
            self._sessions.pop(session_id, None)
            return {"status": "expired"}

        try:
            status = await session.backend.check(session.ticket)
        except DomainError:
            raise
        except Exception:  # noqa: BLE001
            # 单次轮询失败视为等待，由调用方继续轮询
            return {"status": "waiting"}

        if status.status == "success":
            session.confirmed = True
        return {"status": status.status}

    async def finish_login(self, session_id: str) -> QrLoginResult:
        """确认登录成功：换凭据、加密入库。返回账号信息。"""
        session = self._sessions.get(session_id)
        if session is None:
            raise DomainError(
                ErrorCode.ACCOUNT_NOT_FOUND,
                f"qr login session not found or expired: {session_id}",
            )

        if not session.confirmed:
            raise DomainError(
                ErrorCode.ACTION_REQUIRED,
                "qr login not yet confirmed; scan the qr code first",
                retryable=False,
            )

        try:
            credential = await session.backend.finish(session.ticket)
        except DomainError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"failed to finalize {session.provider} qr login: "
                f"{exc.__class__.__name__}",
                retryable=False,
            ) from exc

        if not credential or not credential.credential:
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                f"failed to obtain {session.provider} credential",
                retryable=False,
            )

        # 加密后持久化到账号库（复用现有账号体系）
        encrypted = self.cipher.encrypt(credential.credential)
        account_id = await self._upsert_account(
            session.provider, credential.label or f"{session.provider}-qr",
            encrypted,
        )

        self._sessions.pop(session_id, None)
        logger.info(
            "qr login session %s completed -> account %s",
            session_id,
            account_id,
        )
        return QrLoginResult(
            account_id=account_id,
            provider=session.provider,
            label=credential.label or f"{session.provider}-qr",
        )

    async def cancel_login(self, session_id: str) -> dict[str, Any]:
        """主动取消扫码会话（幂等）。"""
        session = self._sessions.pop(session_id, None)
        if session is None:
            return {"session_id": session_id, "cancelled": False}
        try:
            await session.backend.cancel(session.ticket)
        except Exception:  # noqa: BLE001
            logger.warning(
                "cancel backend failed for session %s", session_id,
                exc_info=True,
            )
        return {"session_id": session_id, "cancelled": True}

    # ---- 内部 ----

    async def _upsert_account(
        self, provider: str, label: str, encrypted_credential: str
    ) -> str:
        from panbridge.domain.models import Account

        account_id = uuid.uuid4().hex
        async with session_scope(self.sessionmaker) as session:
            repo = AccountRepository(session)
            account = Account(
                id=account_id,
                provider=provider,
                label=label,
                encrypted_credential=encrypted_credential,
            )
            await repo.add(account)
        return account_id

    def _prune_expired(self) -> None:
        now = time.time()
        for sid in [
            s.session_id
            for s in self._sessions.values()
            if now - s.created_at > self.session_ttl
        ]:
            self._sessions.pop(sid, None)
