"""任务与账号仓库。

领域对象与 ORM 行之间的转换集中在此，
避免 ORM 细节泄漏到应用层。
"""

from __future__ import annotations

import json
from datetime import UTC
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from panbridge.domain.enums import Action, OnConflict, TaskStatus
from panbridge.domain.models import Account, Task
from panbridge.persistence.models import AccountRow, TaskRow


def task_to_domain(row: TaskRow) -> Task:
    return Task(
        id=row.id,
        action=Action(row.action),
        status=TaskStatus(row.status),
        provider=row.provider,
        account_id=row.account_id,
        input_text=row.input_text,
        selection=json.loads(row.selection) if row.selection else None,
        destination=row.destination,
        on_conflict=OnConflict(row.on_conflict),
        idempotency_key=row.idempotency_key,
        error_code=row.error_code,
        error_message=row.error_message,
        retryable=row.retryable,
        attempt=row.attempt,
        max_attempts=row.max_attempts,
        lease_owner=row.lease_owner,
        lease_expires_at=row.lease_expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )


def task_to_dict(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "action": task.action.value,
        "status": task.status.value,
        "provider": task.provider,
        "account_id": task.account_id,
        "selection": task.selection,
        "destination": task.destination,
        "on_conflict": task.on_conflict.value,
        "error_code": task.error_code,
        "error_message": task.error_message,
        "retryable": task.retryable,
        "attempt": task.attempt,
        "max_attempts": task.max_attempts,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "completed_at": (
            task.completed_at.isoformat() if task.completed_at else None
        ),
    }


def account_to_domain(row: AccountRow) -> Account:
    return Account(
        id=row.id,
        provider=row.provider,
        label=row.label,
        encrypted_credential=row.encrypted_credential,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class TaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, task: Task) -> Task:
        row = TaskRow(
            id=task.id,
            action=task.action.value,
            status=task.status.value,
            provider=task.provider,
            account_id=task.account_id,
            input_text=task.input_text,
            selection=json.dumps(task.selection) if task.selection else None,
            destination=task.destination,
            on_conflict=task.on_conflict.value,
            idempotency_key=task.idempotency_key,
            attempt=task.attempt,
            max_attempts=task.max_attempts,
        )
        self._session.add(row)
        await self._session.flush()
        return task_to_domain(row)

    async def get(self, task_id: str) -> Task | None:
        row = await self._session.get(TaskRow, task_id)
        return task_to_domain(row) if row else None

    async def list(
        self, limit: int = 50, offset: int = 0
    ) -> list[Task]:
        stmt = (
            select(TaskRow)
            .order_by(TaskRow.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [task_to_domain(r) for r in rows]

    async def find_by_idempotency_key(self, key: str) -> Task | None:
        stmt = select(TaskRow).where(TaskRow.idempotency_key == key)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return task_to_domain(row) if row else None

    async def update(self, task: Task) -> None:
        row = await self._session.get(TaskRow, task.id)
        if row is None:
            return
        row.status = task.status.value
        row.provider = task.provider
        row.error_code = task.error_code
        row.error_message = task.error_message
        row.retryable = task.retryable
        row.attempt = task.attempt
        row.lease_owner = task.lease_owner
        row.lease_expires_at = task.lease_expires_at
        row.completed_at = task.completed_at
        await self._session.flush()

    async def claim_next(self, owner: str, lease_seconds: int) -> Task | None:
        """租约式领取一个待执行任务（单 Worker 可靠执行）。

        领取条件：queued，或 resolving 但租约已过期（可恢复中断任务）。
        """
        from datetime import datetime, timedelta

        from sqlalchemy import or_

        now = datetime.now(UTC)
        # queued 或 (resolving 且租约过期)
        expired = TaskRow.lease_expires_at.isnot(None) & (
            TaskRow.lease_expires_at < now
        )
        stmt = (
            select(TaskRow)
            .where(
                or_(
                    TaskRow.status == "queued",
                    (TaskRow.status == "resolving") & expired,
                )
            )
            .order_by(TaskRow.created_at.asc())
            .limit(1)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        row.status = "resolving"
        row.lease_owner = owner
        row.lease_expires_at = now + timedelta(seconds=lease_seconds)
        row.attempt += 1
        await self._session.flush()
        return task_to_domain(row)


class AccountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, account: Account) -> Account:
        row = AccountRow(
            id=account.id,
            provider=account.provider,
            label=account.label,
            encrypted_credential=account.encrypted_credential,
        )
        self._session.add(row)
        await self._session.flush()
        return account_to_domain(row)

    async def get(self, account_id: str) -> Account | None:
        row = await self._session.get(AccountRow, account_id)
        return account_to_domain(row) if row else None

    async def list(self) -> list[Account]:
        rows = (
            (await self._session.execute(select(AccountRow)))
            .scalars()
            .all()
        )
        return [account_to_domain(r) for r in rows]

    async def delete(self, account_id: str) -> None:
        row = await self._session.get(AccountRow, account_id)
        if row is not None:
            await self._session.delete(row)
            await self._session.flush()

    async def get_default(self, provider: str) -> Account | None:
        """返回指定 provider 最近创建的一个账号。"""
        stmt = (
            select(AccountRow)
            .where(AccountRow.provider == provider)
            .order_by(AccountRow.created_at.desc())
            .limit(1)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return account_to_domain(row) if row else None
