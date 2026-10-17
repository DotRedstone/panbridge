"""API 依赖：鉴权等。"""

from __future__ import annotations

from fastapi import Header, HTTPException

from panbridge.infrastructure.config import Settings


def require_api_key(settings: Settings):
    """返回一个 FastAPI 依赖，校验 X-API-Key。"""

    async def _dep(x_api_key: str | None = Header(default=None)) -> None:
        if not settings.api_key:
            # 未配置 API Key 时，仅本机无鉴权（开发模式）
            return
        if x_api_key != settings.api_key:
            raise HTTPException(status_code=401, detail="invalid api key")

    return _dep
