"""panbridge 应用入口。

只负责组装：创建配置、数据库、注册 Provider、下载器、应用服务，
并暴露 FastAPI app 与 Worker。业务逻辑不在本文件堆积。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from panbridge.api.router import build_router
from panbridge.application.service import ApplicationService
from panbridge.domain.errors import DomainError
from panbridge.downloads.http import HttpDownloader
from panbridge.infrastructure.config import Settings
from panbridge.infrastructure.crypto import CredentialCipher
from panbridge.persistence import (
    create_engine,
    create_sessionmaker,
    init_db,
)
from panbridge.providers.quark.mock import MockProvider
from panbridge.providers.quark.provider import QuarkProvider
from panbridge.providers.registry import ProviderRegistry
from panbridge.workers.worker import Worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)


def build_app(settings: Settings | None = None) -> tuple[FastAPI, Worker]:
    settings = settings or Settings.from_env()

    engine = create_engine(settings.database_url)
    sessionmaker = create_sessionmaker(engine)

    registry = ProviderRegistry()
    registry.register(MockProvider())
    # 真实夸克 Provider 需要解密器解密账号 Cookie
    cipher = CredentialCipher(settings.secret_key) if settings.secret_key else None
    registry.register(QuarkProvider(cipher=cipher))

    downloader = HttpDownloader()

    service = ApplicationService(registry, sessionmaker, downloader=downloader)

    worker = Worker(
        registry,
        sessionmaker,
        lease_seconds=settings.worker_lease_seconds,
        max_attempts=settings.max_retry_attempts,
        downloader=downloader,
        download_dir=settings.download_dir,
    )

    app = FastAPI(title="PanBridge", version="0.1.0")
    app.state.settings = settings
    app.state.sessionmaker = sessionmaker
    app.state.worker = worker

    @app.on_event("startup")
    async def _startup() -> None:
        await init_db(engine)

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request: Request, exc: DomainError):
        return JSONResponse(
            status_code=_status_for(exc),
            content={
                "code": exc.code.value,
                "message": exc.message,
                "retryable": exc.retryable,
                "details": exc.details,
                "request_id": getattr(request.state, "request_id", None),
            },
        )

    app.include_router(build_router(service, settings))

    return app, worker


def _status_for(exc: DomainError) -> int:
    from panbridge.domain.errors import ErrorCode

    mapping = {
        ErrorCode.UNAUTHORIZED: 401,
        ErrorCode.TASK_NOT_FOUND: 404,
        ErrorCode.ACCOUNT_NOT_FOUND: 404,
        ErrorCode.INVALID_SHARE: 422,
        ErrorCode.INVALID_PASSWORD: 422,
        ErrorCode.INVALID_REQUEST: 400,
        ErrorCode.IDEMPOTENCY_CONFLICT: 409,
        ErrorCode.UNSUPPORTED_PROVIDER: 422,
        ErrorCode.UNSUPPORTED_OPERATION: 422,
        ErrorCode.TASK_NOT_CANCELLABLE: 409,
        ErrorCode.TASK_NOT_RETRYABLE: 409,
        ErrorCode.RATE_LIMITED: 429,
    }
    return mapping.get(exc.code, 500)


app, worker = build_app()
