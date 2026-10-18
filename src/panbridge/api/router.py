"""API 路由。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from panbridge.api.dependencies import require_api_key
from panbridge.api.schemas import (
    AccountCreateRequest,
    AccountResponse,
    ErrorResponse,
    InspectRequest,
    InspectResponse,
    LoginStatusResponse,
    LogoutResponse,
    QrCancelResponse,
    QrLoginStartResponse,
    QrLoginStatusResponse,
    TaskCreateRequest,
    TaskFileResponse,
    TaskFilesResponse,
    TaskListResponse,
    TaskResponse,
)
from panbridge.application.service import ApplicationService, CreateTaskInput
from panbridge.domain.enums import Action
from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.domain.models import Account
from panbridge.infrastructure.config import Settings
from panbridge.infrastructure.crypto import CredentialCipher
from panbridge.persistence import task_to_dict


def build_router(
    service: ApplicationService,
    settings: Settings,
    auth_service=None,
) -> APIRouter:
    router = APIRouter()
    auth = require_api_key(settings)

    def _error(exc: DomainError, request_id: str) -> JSONResponse:
        return JSONResponse(
            status_code=_status_for(exc),
            content=ErrorResponse(
                code=exc.code.value,
                message=exc.message,
                retryable=exc.retryable,
                details=exc.details,
                request_id=request_id,
            ).model_dump(),
        )

    @router.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @router.get("/v1/providers")
    async def list_providers(_=Depends(auth)) -> dict:
        return {"providers": service.registry.list_meta()}

    @router.post("/v1/shares/inspect", response_model=InspectResponse)
    async def inspect_share(
        req: InspectRequest, _=Depends(auth)
    ) -> InspectResponse:
        result = await service.inspect_share(req.text)
        return InspectResponse(**result)

    @router.post("/v1/tasks", status_code=202, response_model=TaskResponse)
    async def create_task(
        body: TaskCreateRequest,
        request: Request,
        response: Response,
        _=Depends(auth),
    ):
        inp = body.input
        text = inp.get("text", "")
        action_raw = inp.get("action", "inspect")
        try:
            action = Action(action_raw)
        except ValueError as exc:
            raise DomainError(
                ErrorCode.INVALID_REQUEST,
                f"invalid action: {action_raw}",
            ) from exc

        idempotency_key = request.headers.get("Idempotency-Key")

        create_input = CreateTaskInput(
            text=text,
            action=action,
            account_id=inp.get("account_id"),
            selection=inp.get("selection"),
            destination=inp.get("destination"),
            on_conflict=inp.get("on_conflict", "skip"),
            idempotency_key=idempotency_key,
        )
        task = await service.create_task(create_input)
        return TaskResponse(**task_to_dict(task))

    @router.get("/v1/tasks", response_model=TaskListResponse)
    async def list_tasks(limit: int = 50, offset: int = 0, _=Depends(auth)):
        tasks = await service.list_tasks(limit, offset)
        return TaskListResponse(
            items=[TaskResponse(**task_to_dict(t)) for t in tasks]
        )

    @router.get("/v1/tasks/{task_id}", response_model=TaskResponse)
    async def get_task(task_id: str, _=Depends(auth)):
        task = await service.get_task(task_id)
        return TaskResponse(**task_to_dict(task))

    @router.get(
        "/v1/tasks/{task_id}/files", response_model=TaskFilesResponse
    )
    async def get_task_files(task_id: str, _=Depends(auth)):
        files = await service.get_task_files(task_id)
        return TaskFilesResponse(
            items=[TaskFileResponse(**f) for f in files]
        )

    @router.get("/v1/tasks/{task_id}/files/{filename}")
    async def download_task_file(
        task_id: str, filename: str, _=Depends(auth)
    ):
        """下载任务产出的本地文件。

        文件名仅允许出现在任务 result 中，且必须落在 download_dir 内，
        防止路径穿越。
        """
        files = await service.get_task_files(task_id)
        matched = [f for f in files if f.get("name") == filename]
        if not matched:
            raise HTTPException(status_code=404, detail="file not found")

        download_dir = Path(settings.download_dir).resolve()
        target = (download_dir / filename).resolve()
        # 路径穿越防护：目标必须位于 download_dir 内
        if download_dir not in target.parents:
            raise HTTPException(status_code=403, detail="forbidden path")

        if not target.is_file():
            raise HTTPException(status_code=404, detail="file not found")

        return FileResponse(str(target), filename=filename)

    @router.post("/v1/tasks/{task_id}/cancel", response_model=TaskResponse)
    async def cancel_task(task_id: str, _=Depends(auth)):
        task = await service.cancel_task(task_id)
        return TaskResponse(**task_to_dict(task))

    @router.post("/v1/tasks/{task_id}/retry", response_model=TaskResponse)
    async def retry_task(task_id: str, _=Depends(auth)):
        task = await service.retry_task(task_id)
        return TaskResponse(**task_to_dict(task))

    @router.get("/v1/accounts", response_model=list[AccountResponse])
    async def list_accounts(_=Depends(auth)):
        accounts = await service.list_accounts()
        return [_account_to_response(a) for a in accounts]

    @router.post("/v1/accounts", status_code=201, response_model=AccountResponse)
    async def create_account(
        body: AccountCreateRequest, _=Depends(auth)
    ):
        if not settings.secret_key:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "PANBRIDGE_SECRET_KEY 未配置，无法加密存储账号凭据。",
                retryable=False,
            )
        cipher = CredentialCipher(settings.secret_key)
        encrypted = cipher.encrypt(body.credential)
        account = await service.add_account(
            body.provider, body.label, encrypted
        )
        return _account_to_response(account)

    # ---- 扫码登录（为 AstrBot 集成预留，无 WebUI）----

    @router.post(
        "/v1/auth/qr/start",
        response_model=QrLoginStartResponse,
    )
    async def start_qr_login(
        provider: str = "quark", _=Depends(auth)
    ):
        if auth_service is None:
            raise DomainError(
                ErrorCode.UNSUPPORTED_OPERATION,
                "qr login not available (credential cipher not configured)",
                retryable=False,
            )
        result = await auth_service.start_qr_login(provider)
        return QrLoginStartResponse(**result)

    @router.get(
        "/v1/auth/qr/{session_id}",
        response_model=QrLoginStatusResponse,
    )
    async def check_qr_login(session_id: str, _=Depends(auth)):
        if auth_service is None:
            raise DomainError(
                ErrorCode.UNSUPPORTED_OPERATION,
                "qr login not available (credential cipher not configured)",
                retryable=False,
            )
        status = await auth_service.check_qr_login(session_id)
        if status.get("status") == "success":
            # 扫码成功，完成登录并返回账号
            result = await auth_service.finish_qr_login(session_id)
            account = await service.get_account(result["account_id"])
            return QrLoginStatusResponse(
                session_id=session_id,
                status="success",
                account_id=result["account_id"],
                account=_account_to_response(account),
            )
        return QrLoginStatusResponse(
            session_id=session_id,
            status=status.get("status", "waiting"),
        )

    @router.post(
        "/v1/auth/qr/{session_id}/cancel",
        response_model=QrCancelResponse,
    )
    async def cancel_qr_login(session_id: str, _=Depends(auth)):
        if auth_service is None:
            raise DomainError(
                ErrorCode.UNSUPPORTED_OPERATION,
                "qr login not available (credential cipher not configured)",
                retryable=False,
            )
        result = await auth_service.cancel_qr_login(session_id)
        return QrCancelResponse(**result)

    @router.get("/v1/auth/status", response_model=LoginStatusResponse)
    async def login_status(provider: str = "quark", _=Depends(auth)):
        if auth_service is None:
            raise DomainError(
                ErrorCode.UNSUPPORTED_OPERATION,
                "auth not available (credential cipher not configured)",
                retryable=False,
            )
        status = await auth_service.login_status(provider)
        account = status.get("account")
        return LoginStatusResponse(
            logged_in=status.get("logged_in", False),
            expired=status.get("expired", False),
            account=_account_to_response(account) if account else None,
        )

    @router.post("/v1/auth/logout", response_model=LogoutResponse)
    async def logout(provider: str = "quark", _=Depends(auth)):
        if auth_service is None:
            raise DomainError(
                ErrorCode.UNSUPPORTED_OPERATION,
                "auth not available (credential cipher not configured)",
                retryable=False,
            )
        result = await auth_service.logout(provider)
        return LogoutResponse(**result)

    return router


def _account_to_response(a: Account) -> AccountResponse:
    return AccountResponse(
        id=a.id,
        provider=a.provider,
        label=a.label,
        created_at=a.created_at.isoformat() if a.created_at else None,
        updated_at=a.updated_at.isoformat() if a.updated_at else None,
    )


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
