"""任务租约与恢复测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from panbridge.persistence import (
    TaskRepository,
    create_engine,
    create_sessionmaker,
    init_db,
    session_scope,
)


@pytest_asyncio.fixture
async def repo_env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await init_db(engine)
    sessionmaker = create_sessionmaker(engine)
    yield sessionmaker
    await engine.dispose()


@pytest.mark.asyncio
async def test_claim_queued_task(repo_env):
    from panbridge.application.service import ApplicationService, CreateTaskInput
    from panbridge.domain.enums import Action, TaskStatus
    from panbridge.providers.quark.mock import MockProvider
    from panbridge.providers.registry import ProviderRegistry

    reg = ProviderRegistry()
    reg.register(MockProvider())
    svc = ApplicationService(reg, repo_env)

    task = await svc.create_task(
        CreateTaskInput(text="https://mock.local/s/mock001", action=Action.INSPECT)
    )

    async with session_scope(repo_env) as s:
        claimed = await TaskRepository(s).claim_next("w1", 300)
        assert claimed is not None
        assert claimed.id == task.id
        assert claimed.status == TaskStatus.RESOLVING


@pytest.mark.asyncio
async def test_claim_recovers_expired_lease(repo_env):
    from panbridge.application.service import ApplicationService, CreateTaskInput
    from panbridge.domain.enums import Action
    from panbridge.persistence.models import TaskRow
    from panbridge.providers.quark.mock import MockProvider
    from panbridge.providers.registry import ProviderRegistry

    reg = ProviderRegistry()
    reg.register(MockProvider())
    svc = ApplicationService(reg, repo_env)

    task = await svc.create_task(
        CreateTaskInput(text="https://mock.local/s/mock001", action=Action.INSPECT)
    )

    # 模拟 worker A 领取后崩溃：状态 resolving，租约已过期
    async with session_scope(repo_env) as s:
        row = await s.get(TaskRow, task.id)
        row.status = "resolving"
        row.lease_owner = "dead-worker"
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=10)

    # worker B 应能恢复该任务
    async with session_scope(repo_env) as s:
        claimed = await TaskRepository(s).claim_next("w2", 300)
        assert claimed is not None
        assert claimed.id == task.id
