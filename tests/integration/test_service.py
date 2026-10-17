"""应用服务集成测试：任务生命周期。"""

from __future__ import annotations

import pytest

from panbridge.application.service import CreateTaskInput
from panbridge.domain.enums import Action, TaskStatus
from panbridge.domain.errors import DomainError, ErrorCode


@pytest.mark.asyncio
async def test_inspect_share(service):
    result = await service.inspect_share(
        "https://mock.local/s/mock001 提取码: 1234"
    )
    assert result["provider"] == "mock"
    assert result["title"] == "mock-share-mock001"
    assert len(result["files"]) == 3


@pytest.mark.asyncio
async def test_invalid_share_inspect(service):
    with pytest.raises(DomainError) as exc:
        await service.inspect_share("https://mock.local/s/invalid001")
    assert exc.value.code == ErrorCode.INVALID_SHARE


@pytest.mark.asyncio
async def test_create_task_and_query(service):
    task = await service.create_task(
        CreateTaskInput(
            text="https://mock.local/s/mock001",
            action=Action.INSPECT,
        )
    )
    assert task.status == TaskStatus.QUEUED

    fetched = await service.get_task(task.id)
    assert fetched.id == task.id
    assert fetched.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_idempotency_key_reuse(service):
    inp = CreateTaskInput(
        text="https://mock.local/s/mock001",
        action=Action.INSPECT,
        idempotency_key="key-1",
    )
    t1 = await service.create_task(inp)
    t2 = await service.create_task(inp)
    assert t1.id == t2.id


@pytest.mark.asyncio
async def test_idempotency_conflict(service):
    await service.create_task(
        CreateTaskInput(
            text="https://mock.local/s/mock001",
            action=Action.INSPECT,
            idempotency_key="key-2",
        )
    )
    with pytest.raises(DomainError) as exc:
        await service.create_task(
            CreateTaskInput(
                text="https://mock.local/s/other",
                action=Action.INSPECT,
                idempotency_key="key-2",
            )
        )
    assert exc.value.code == ErrorCode.IDEMPOTENCY_CONFLICT


@pytest.mark.asyncio
async def test_cancel_and_double_cancel(service):
    task = await service.create_task(
        CreateTaskInput(
            text="https://mock.local/s/mock001",
            action=Action.INSPECT,
        )
    )
    cancelled = await service.cancel_task(task.id)
    assert cancelled.status == TaskStatus.CANCELLED

    with pytest.raises(DomainError) as exc:
        await service.cancel_task(task.id)
    assert exc.value.code == ErrorCode.TASK_NOT_CANCELLABLE


@pytest.mark.asyncio
async def test_task_not_found(service):
    with pytest.raises(DomainError) as exc:
        await service.get_task("nonexistent")
    assert exc.value.code == ErrorCode.TASK_NOT_FOUND


@pytest.mark.asyncio
async def test_add_account(service):
    account = await service.add_account("mock", "test", "cred")
    assert account.provider == "mock"
    accounts = await service.list_accounts()
    assert len(accounts) == 1
