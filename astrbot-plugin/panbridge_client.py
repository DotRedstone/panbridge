"""PanBridge HTTP API 客户端。

独立于 AstrBot 框架的纯 httpx 封装，方便单元测试与复用。
PanBridge 服务端接口契约见 ../docs/api.md。
"""

from __future__ import annotations

from typing import Any

import httpx


class PanBridgeError(Exception):
    """PanBridge 业务错误。"""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class PanBridgeClient:
    """封装 PanBridge 的 /v1 接口。

    - base_url: PanBridge 服务地址，如 http://127.0.0.1:8000
    - api_key: 鉴权密钥（对应服务端 PANBRIDGE_API_KEY，未配置时留空）
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        # transport 仅供测试注入（httpx.MockTransport），生产为 None。
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        h = {"accept": "application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h

    async def _request(
        self, method: str, path: str, *, json: Any | None = None, **kwargs
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self._transport
        ) as client:
            resp = await client.request(
                method, url, headers=self._headers(), json=json, **kwargs
            )
        if resp.status_code >= 400:
            # 优先解析统一错误结构
            try:
                body = resp.json()
                code = body.get("code")
                message = body.get("message") or resp.text
            except Exception:
                code = None
                message = resp.text or f"HTTP {resp.status_code}"
            raise PanBridgeError(message, code)
        return resp.json()

    # ---- 解析与任务 ----

    async def inspect(self, text: str) -> dict[str, Any]:
        """解析分享，预览文件列表。"""
        return await self._request("POST", "/v1/shares/inspect", json={"text": text})

    async def create_task(
        self,
        text: str,
        action: str,
        account_id: str | None = None,
        selection: list[str] | None = None,
        destination: str | None = None,
        on_conflict: str = "skip",
    ) -> dict[str, Any]:
        """创建转存/下载任务，返回任务对象（含 id）。"""
        inp: dict[str, Any] = {
            "text": text,
            "action": action,
            "on_conflict": on_conflict,
        }
        if account_id:
            inp["account_id"] = account_id
        if selection:
            inp["selection"] = selection
        if destination:
            inp["destination"] = destination
        return await self._request("POST", "/v1/tasks", json={"input": inp})

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/tasks/{task_id}")

    async def list_tasks(self, limit: int = 10, offset: int = 0) -> dict[str, Any]:
        return await self._request(
            "GET", f"/v1/tasks?limit={limit}&offset={offset}"
        )

    async def cancel_task(self, task_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/v1/tasks/{task_id}/cancel")

    async def retry_task(self, task_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/v1/tasks/{task_id}/retry")

    async def get_task_files(self, task_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/tasks/{task_id}/files")

    # ---- 账号与认证 ----

    async def list_accounts(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/v1/accounts")

    async def start_qr_login(self, provider: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"/v1/auth/qr/start?provider={provider}"
        )

    async def check_qr_login(self, session_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/auth/qr/{session_id}")

    async def login_status(self, provider: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/auth/status?provider={provider}")

    async def list_providers(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/providers")
