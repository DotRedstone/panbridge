"""统一认证服务门面。

把「扫码登录」「登录状态查询」「退出登录」聚合为单一入口，
供 CLI 与 REST API 复用，避免各端重复编排。

- 扫码登录：按 provider 路由到对应后端（夸克/阿里云盘/115 网盘）。
- 登录状态：基于数据库中的已登录账号 + 真实校验（validate_account）。
- 退出登录：删除本地已保存的账号凭据（不调用远端登出）。
- 凭据失效：校验失败时返回 expired，提示重新扫码，不静默使用失效 Cookie。
"""

from __future__ import annotations

import logging
from typing import Any

from panbridge.application.auth import QrAuthService
from panbridge.application.service import ApplicationService
from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)


class AuthService:
    """认证门面：登录 / 状态 / 退出。"""

    def __init__(
        self,
        service: ApplicationService,
        registry: ProviderRegistry,
        qr_auth: QrAuthService,
    ) -> None:
        self._service = service
        self._registry = registry
        self._qr_auth = qr_auth

    # ---- 扫码登录 ----

    def supported_qr_providers(self) -> list[str]:
        return self._qr_auth.supported_providers()

    async def start_qr_login(self, provider: str = "quark") -> dict[str, str]:
        return await self._qr_auth.start_login(provider)

    async def check_qr_login(self, session_id: str) -> dict[str, Any]:
        return await self._qr_auth.check_login(session_id)

    async def finish_qr_login(self, session_id: str) -> dict[str, Any]:
        result = await self._qr_auth.finish_login(session_id)
        return {
            "account_id": result.account_id,
            "provider": result.provider,
            "label": result.label,
            "logged_in": result.logged_in,
        }

    async def cancel_qr_login(self, session_id: str) -> dict[str, Any]:
        return await self._qr_auth.cancel_login(session_id)

    # ---- 登录状态 ----

    async def login_status(self, provider: str = "quark") -> dict[str, Any]:
        """查询当前登录状态。

        返回 {"logged_in": bool, "account": {...} | None, "expired": bool}。
        会真实校验凭据是否有效，失效时标记 expired 并提示重新扫码。
        """
        account = await self._service.get_default_account(provider)
        if account is None:
            return {"logged_in": False, "account": None, "expired": False}

        # 真实校验凭据，失效时不静默当作有效
        provider_impl = self._registry.get(provider)
        try:
            valid = await provider_impl.validate_account(account)
        except DomainError as exc:
            if exc.code == ErrorCode.ACCOUNT_EXPIRED:
                valid = False
            else:
                # 其他错误（网络等）不误判为失效，仅提示未知
                return {
                    "logged_in": True,
                    "account": self._account_view(account),
                    "expired": False,
                    "verify_error": exc.code.value,
                }
        if not valid:
            return {
                "logged_in": False,
                "account": self._account_view(account),
                "expired": True,
            }

        return {
            "logged_in": True,
            "account": self._account_view(account),
            "expired": False,
        }

    # ---- 退出登录 ----

    async def logout(self, provider: str = "quark") -> dict[str, Any]:
        """退出登录：删除本地保存的账号凭据。

        删除所有该 provider 的账号，返回删除数量。
        """
        accounts = await self._service.list_accounts()
        removed = 0
        for account in accounts:
            if account.provider == provider:
                await self._service.delete_account(account.id)
                removed += 1
        logger.info("logout provider=%s removed=%d", provider, removed)
        return {"provider": provider, "removed": removed}

    # ---- 内部 ----

    @staticmethod
    def _account_view(account) -> dict[str, Any]:
        return {
            "id": account.id,
            "provider": account.provider,
            "label": account.label,
            "created_at": (
                account.created_at.isoformat() if account.created_at else None
            ),
        }
