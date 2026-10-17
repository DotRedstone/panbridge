"""应用层：业务用例编排。

负责组织 Provider、下载层与持久化层的协作，
不包含具体网盘 API 的 HTTP 请求细节。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from panbridge.application.link_extraction import extract_share
from panbridge.domain.enums import Action, OnConflict, TaskStatus
from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.domain.models import Account, Task
from panbridge.downloads.base import Downloader
from panbridge.persistence import (
    AccountRepository,
    TaskRepository,
    session_scope,
)
from panbridge.providers.registry import ProviderRegistry


@dataclass
class CreateTaskInput:
    text: str
    action: Action
    account_id: str | None = None
    selection: list[str] | None = None
    destination: str | None = None
    on_conflict: str = "skip"
    idempotency_key: str | None = None


def compute_request_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ApplicationService:
    """核心用例服务，持有依赖但不执行耗时任务。"""

    def __init__(
        self,
        registry: ProviderRegistry,
        sessionmaker,
        downloader: Downloader | None = None,
    ) -> None:
        self.registry = registry
        self.sessionmaker = sessionmaker
        self.downloader = downloader

    # ---- Inspect ----

    async def inspect_share(self, text: str) -> dict[str, Any]:
        extracted = extract_share(text)
        provider = self.registry.get(extracted.provider)
        share = await provider.inspect_share(
            extracted.share_id, extracted.password
        )
        return {
            "provider": extracted.provider,
            "share_id": extracted.share_id,
            "title": share.title,
            "files": [
                {
                    "file_id": f.file_id,
                    "name": f.name,
                    "is_dir": f.is_dir,
                    "size": f.size,
                }
                for f in share.files
            ],
        }

    # ---- Create task ----

    async def create_task(self, inp: CreateTaskInput) -> Task:
        extracted = extract_share(inp.text)

        async with session_scope(self.sessionmaker) as session:
            repo = TaskRepository(session)

            # 幂等处理
            if inp.idempotency_key:
                existing = await repo.find_by_idempotency_key(
                    inp.idempotency_key
                )
                if existing:
                    # 相同 key 不同请求体 -> 冲突
                    if existing.input_text != inp.text:
                        raise DomainError(
                            ErrorCode.IDEMPOTENCY_CONFLICT,
                            "idempotency key reused with different request",
                        )
                    return existing

            # 校验账号存在（若指定）
            if inp.account_id:
                acct_repo = AccountRepository(session)
                account = await acct_repo.get(inp.account_id)
                if account is None:
                    raise DomainError(
                        ErrorCode.ACCOUNT_NOT_FOUND,
                        f"account not found: {inp.account_id}",
                    )

            task = Task(
                id=_new_task_id(),
                action=inp.action,
                provider=extracted.provider,
                account_id=inp.account_id,
                input_text=inp.text,
                selection=inp.selection,
                destination=inp.destination,
                on_conflict=OnConflict(inp.on_conflict),
                idempotency_key=inp.idempotency_key,
                status=TaskStatus.QUEUED,
            )
            return await repo.add(task)

    # ---- Query ----

    async def get_task(self, task_id: str) -> Task:
        async with session_scope(self.sessionmaker) as session:
            task = await TaskRepository(session).get(task_id)
            if task is None:
                raise DomainError(
                    ErrorCode.TASK_NOT_FOUND, f"task not found: {task_id}"
                )
            return task

    async def list_tasks(self, limit: int = 50, offset: int = 0) -> list[Task]:
        async with session_scope(self.sessionmaker) as session:
            return await TaskRepository(session).list(limit, offset)

    # ---- Cancel ----

    async def cancel_task(self, task_id: str) -> Task:
        async with session_scope(self.sessionmaker) as session:
            repo = TaskRepository(session)
            task = await repo.get(task_id)
            if task is None:
                raise DomainError(
                    ErrorCode.TASK_NOT_FOUND, f"task not found: {task_id}"
                )
            terminal = (
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            )
            if task.status in terminal:
                raise DomainError(
                    ErrorCode.TASK_NOT_CANCELLABLE,
                    f"task {task_id} is in terminal state {task.status.value}",
                )
            task.status = TaskStatus.CANCELLED
            await repo.update(task)
            return task

    # ---- Retry ----

    async def retry_task(self, task_id: str) -> Task:
        async with session_scope(self.sessionmaker) as session:
            repo = TaskRepository(session)
            task = await repo.get(task_id)
            if task is None:
                raise DomainError(
                    ErrorCode.TASK_NOT_FOUND, f"task not found: {task_id}"
                )
            if task.status not in (TaskStatus.FAILED, TaskStatus.WAITING_AUTH):
                raise DomainError(
                    ErrorCode.TASK_NOT_RETRYABLE,
                    f"task {task_id} is not retryable (status={task.status.value})",
                )
            task.status = TaskStatus.QUEUED
            task.error_code = None
            task.error_message = None
            task.lease_owner = None
            task.lease_expires_at = None
            await repo.update(task)
            return task

    # ---- Accounts ----

    async def add_account(
        self, provider: str, label: str, credential: str
    ) -> Account:
        # 校验 provider 存在
        self.registry.get(provider)
        async with session_scope(self.sessionmaker) as session:
            repo = AccountRepository(session)
            account = Account(
                id=_new_account_id(),
                provider=provider,
                label=label,
                encrypted_credential=credential,  # 加密由上层完成
            )
            return await repo.add(account)

    async def list_accounts(self) -> list[Account]:
        async with session_scope(self.sessionmaker) as session:
            return await AccountRepository(session).list()

    async def get_account(self, account_id: str) -> Account:
        async with session_scope(self.sessionmaker) as session:
            account = await AccountRepository(session).get(account_id)
            if account is None:
                raise DomainError(
                    ErrorCode.ACCOUNT_NOT_FOUND,
                    f"account not found: {account_id}",
                )
            return account

    async def delete_account(self, account_id: str) -> None:
        """删除账号（退出登录）。

        账号不存在时静默返回（幂等），避免重复登出报错。
        """
        async with session_scope(self.sessionmaker) as session:
            repo = AccountRepository(session)
            if await repo.get(account_id) is None:
                return
            await repo.delete(account_id)

    async def get_default_account(self, provider: str) -> Account | None:
        """返回指定 provider 最近创建的一个账号（作为默认登录账号）。"""
        async with session_scope(self.sessionmaker) as session:
            return await AccountRepository(session).get_default(provider)


def _new_task_id() -> str:
    import uuid

    return uuid.uuid4().hex


def _new_account_id() -> str:
    import uuid

    return uuid.uuid4().hex
