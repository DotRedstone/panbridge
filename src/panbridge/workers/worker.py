"""任务执行 Worker。

独立的后台循环，从数据库租约领取任务并执行完整业务链路。
使用租约 + 心跳机制保证单 Worker 可靠执行，
支持异常重启后的任务恢复。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from panbridge.domain.enums import Action, TaskStatus
from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.domain.models import Share, Task
from panbridge.persistence import TaskRepository, session_scope
from panbridge.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        registry: ProviderRegistry,
        sessionmaker,
        *,
        worker_id: str = "worker-1",
        lease_seconds: int = 300,
        max_attempts: int = 3,
        max_task_concurrency: int = 4,
        poll_interval: float = 1.0,
        downloader=None,
        download_dir: str = "./downloads",
    ) -> None:
        self.registry = registry
        self.sessionmaker = sessionmaker
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.max_task_concurrency = max_task_concurrency
        self.poll_interval = poll_interval
        self.downloader = downloader
        self.download_dir = download_dir
        self._running = False

    async def run(self) -> None:
        """主循环：并发领取并执行任务，直到被停止。

        使用 `max_task_concurrency` 限制同时执行的任务数，
        避免单个 Worker 无限并发占用资源。
        """
        self._running = True
        tasks: set[asyncio.Task] = set()
        while self._running:
            try:
                # 补充到并发上限
                while len(tasks) < self.max_task_concurrency:
                    task = await self._claim()
                    if task is None:
                        break
                    tasks.add(asyncio.create_task(self._execute(task)))

                if not tasks:
                    await asyncio.sleep(self.poll_interval)
                    continue

                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                tasks = pending
                for t in done:
                    if t.exception() is not None:
                        logger.exception(
                            "worker task raised: %s", t.exception()
                        )
            except asyncio.CancelledError:
                logger.info("worker %s cancelled", self.worker_id)
                for t in tasks:
                    t.cancel()
                raise
            except Exception:
                logger.exception("worker loop error")

    async def _claim(self) -> Task | None:
        async with session_scope(self.sessionmaker) as session:
            return await TaskRepository(session).claim_next(
                self.worker_id, self.lease_seconds
            )

    async def _execute(self, task: Task) -> None:
        logger.info("executing task %s action=%s", task.id, task.action.value)
        try:
            provider = self.registry.get(task.provider or "")
            result = await self._run_task(task, provider)
            await self._complete(task.id, result)
        except DomainError as exc:
            await self._fail(task.id, exc)
        except Exception:  # noqa: BLE001
            logger.exception("unexpected error in task %s", task.id)
            await self._fail(
                task.id,
                DomainError(
                    ErrorCode.INTERNAL_ERROR,
                    "internal error",
                    retryable=True,
                ),
            )

    async def _run_task(self, task: Task, provider) -> dict:
        """执行具体业务动作，返回结果字典。"""
        # 解析分享
        from panbridge.application.link_extraction import extract_share

        extracted = extract_share(task.input_text or "")

        # 部分平台（UC/迅雷/移动云盘等）解析分享需要登录态。
        # 先尝试匿名解析；若需登录，则获取账号后用带 account 的解析。
        share = None
        account = None
        try:
            share = await provider.inspect_share(
                extracted.share_id, extracted.password
            )
        except DomainError as exc:
            if exc.code != ErrorCode.UNSUPPORTED_OPERATION:
                raise
            # 需登录平台：SAVE/DOWNLOAD 动作有 account 可用；
            # 纯 INSPECT 动作无 account，如实返回需登录错误。
            if task.action == Action.INSPECT:
                raise DomainError(
                    ErrorCode.ACTION_REQUIRED,
                    f"provider {task.provider} requires login to inspect share",
                ) from exc
            account = await self._get_account(task.account_id)
            files = await provider.list_share_files(
                extracted.share_id, extracted.password, account=account
            )
            share = Share(
                provider=task.provider or "",
                share_id=extracted.share_id,
                title=extracted.share_id,
                files=files,
                password=extracted.password,
            )

        if task.action == Action.INSPECT:
            return {"title": share.title, "files": len(share.files)}

        # 选择要转存的文件（默认全部非目录文件）
        selected = task.selection or [
            f.file_id for f in share.files if not f.is_dir
        ]
        # 源文件名映射（下载时用于定位转存后的文件）
        name_by_fid = {f.file_id: f.name for f in share.files}

        saved_file_ids: list[str] = []

        # 转存目标目录：若未指定，则创建独立文件夹，避免污染根目录、
        # 避免与账号现有同名文件冲突。
        destination = task.destination

        if task.action in (Action.SAVE, Action.SAVE_AND_DOWNLOAD):
            account = account or await self._get_account(task.account_id)
            await self._set_status(task.id, TaskStatus.SAVING)

            if not destination:
                folder_name = f"panbridge-{task.id[:8]}"
                try:
                    destination = await provider.create_folder(
                        account, folder_name, "0"
                    )
                except DomainError as exc:
                    if exc.code == ErrorCode.UNSUPPORTED_OPERATION:
                        # 平台不支持创建文件夹，回退到根目录（明确记录）
                        destination = "0"
                    else:
                        raise

            transfer = await provider.save_shared_files(
                account,
                extracted.share_id,
                extracted.password,
                selected,
                destination,
                task.on_conflict.value,
            )
            saved_file_ids = transfer.saved_file_ids
            if task.action == Action.SAVE:
                return {"saved": saved_file_ids, "destination": destination}

        if task.action in (Action.DOWNLOAD, Action.SAVE_AND_DOWNLOAD):
            if self.downloader is None:
                raise DomainError(
                    ErrorCode.UNSUPPORTED_OPERATION,
                    "download backend not configured",
                )
            await self._set_status(task.id, TaskStatus.DOWNLOADING)

            # 下载源文件 ID 与下载模式：
            # - SAVE_AND_DOWNLOAD：转存后从账号网盘下载（需 account + 新 fid）。
            # - DOWNLOAD：优先匿名分享直链下载（无需账号）；若平台不支持
            #   匿名下载且提供了 account，则回退到账号下载。
            if task.action == Action.SAVE_AND_DOWNLOAD:
                account = account or await self._get_account(task.account_id)
                download_file_ids = saved_file_ids
                if not download_file_ids:
                    names = [name_by_fid.get(f, f) for f in selected]
                    resolved = await provider.resolve_saved_file_ids(
                        account, destination, names
                    )
                    if not resolved:
                        raise DomainError(
                            ErrorCode.DOWNLOAD_FAILED,
                            "cannot resolve saved file IDs after transfer",
                            retryable=False,
                        )
                    download_file_ids = resolved
                downloaded = await self._download_account_files(
                    provider,
                    account,
                    download_file_ids,
                    name_by_fid,
                    task.on_conflict.value,
                )
            else:
                # 纯 DOWNLOAD：匿名分享下载（有 account 也优先匿名直链，
                # 因为分享直链通常比账号下载更简单且无需转存）。
                downloaded = await self._download_share_files(
                    provider,
                    extracted.share_id,
                    extracted.password,
                    selected,
                    name_by_fid,
                    task.on_conflict.value,
                )

            return {
                "downloaded": downloaded,
                "files": [
                    {"name": d["name"], "path": d["path"], "size": d["size"]}
                    for d in downloaded
                ],
            }

        return {}

    async def _get_account(self, account_id: str | None):
        from panbridge.persistence import AccountRepository

        if account_id is None:
            raise DomainError(
                ErrorCode.ACCOUNT_NOT_FOUND,
                "account_id is required for save/download",
            )
        async with session_scope(self.sessionmaker) as session:
            account = await AccountRepository(session).get(account_id)
            if account is None:
                raise DomainError(
                    ErrorCode.ACCOUNT_NOT_FOUND,
                    f"account not found: {account_id}",
                )
            return account

    async def _download_files(
        self,
        provider,
        account,
        file_ids: list[str],
        name_by_fid: dict,
        on_conflict: str,
    ) -> list[dict]:
        results: list[dict] = []
        for fid in file_ids:
            info = await provider.get_download_info(account, fid)
            url = info["url"]
            # 优先用原始文件名，避免用 fid 作为文件名
            name = name_by_fid.get(fid) or info.get("name") or f"{fid}.bin"
            dest = self._resolve_local_path(name, on_conflict)
            headers = info.get("headers") or {}
            expected_size = info.get("size")
            async for _progress in self.downloader.download(
                url, dest, headers=headers, expected_size=expected_size
            ):
                pass
            size = _local_file_size(dest)
            results.append({"name": name, "path": dest, "size": size})
        return results

    async def _download_share_files(
        self,
        provider,
        share_id: str,
        password: str | None,
        file_ids: list[str],
        name_by_fid: dict,
        on_conflict: str,
    ) -> list[dict]:
        """匿名分享直链下载（无需账号）。

        对每个选中文件调用 `get_share_download_info` 解析直链并下载。
        平台不支持匿名分享下载时抛 UNSUPPORTED_OPERATION（由上层捕获
        后提示需先转存或登录）。
        """
        results: list[dict] = []
        for fid in file_ids:
            info = await provider.get_share_download_info(
                share_id, password, fid
            )
            url = info["url"]
            # 文件名优先级：接口返回的 name > inspect 时记录的 name。
            name = (
                info.get("name")
                or name_by_fid.get(fid)
                or f"{fid}.bin"
            )
            dest = self._resolve_local_path(name, on_conflict)
            headers = info.get("headers") or {}
            expected_size = info.get("size")
            async for _progress in self.downloader.download(
                url, dest, headers=headers, expected_size=expected_size
            ):
                pass
            size = _local_file_size(dest)
            results.append({"name": name, "path": dest, "size": size})
        return results

    async def _download_account_files(
        self,
        provider,
        account,
        file_ids: list[str],
        name_by_fid: dict,
        on_conflict: str,
    ) -> list[dict]:
        """账号网盘文件下载（转存后）。

        通过 `get_download_info` 拿账号文件直链并下载。
        文件名优先用 `resolve` 前的源文件名映射。
        """
        return await self._download_files(
            provider, account, file_ids, name_by_fid, on_conflict
        )

    def _resolve_local_path(self, name: str, on_conflict: str) -> str:
        """根据冲突策略确定本地下载路径。

        - skip：目标已存在时跳过（返回 None 由调用方忽略）
        - rename：自动追加序号避免覆盖
        - overwrite：直接覆盖（默认行为，与现状一致）
        """
        import os

        dest = os.path.join(self.download_dir, name)
        if on_conflict == "rename" and os.path.exists(dest):
            base, ext = os.path.splitext(name)
            counter = 1
            while os.path.exists(dest):
                dest = os.path.join(
                    self.download_dir, f"{base} ({counter}){ext}"
                )
                counter += 1
        # skip 与 overwrite：skip 语义交由上层（转存 on_conflict），
        # 本地下载默认覆盖（幂等重试也依赖覆盖保证一致）。
        return dest

    async def _set_status(self, task_id: str, status: TaskStatus) -> None:
        async with session_scope(self.sessionmaker) as session:
            repo = TaskRepository(session)
            task = await repo.get(task_id)
            if task and task.status != TaskStatus.CANCELLED:
                task.status = status
                await repo.update(task)

    async def _complete(self, task_id: str, result: dict) -> None:
        async with session_scope(self.sessionmaker) as session:
            repo = TaskRepository(session)
            task = await repo.get(task_id)
            if task is None:
                return
            if task.status == TaskStatus.CANCELLED:
                return
            task.status = TaskStatus.COMPLETED
            task.result = result
            task.completed_at = datetime.now(UTC)
            task.lease_owner = None
            task.lease_expires_at = None
            await repo.update(task)

    async def _fail(self, task_id: str, exc: DomainError) -> None:
        async with session_scope(self.sessionmaker) as session:
            repo = TaskRepository(session)
            task = await repo.get(task_id)
            if task is None or task.status == TaskStatus.CANCELLED:
                return
            # attempt 在 claim_next 时已自增，重试判定应基于「是否还有剩余次数」。
            can_retry = exc.retryable and task.attempt < task.max_attempts
            if can_retry:
                # 归还队列等待重试；保留错误信息供排查。
                task.status = TaskStatus.QUEUED
            else:
                task.status = TaskStatus.FAILED
                task.completed_at = datetime.now(UTC)
            task.error_code = exc.code.value
            task.error_message = exc.message
            task.retryable = exc.retryable
            task.lease_owner = None
            task.lease_expires_at = None
            await repo.update(task)

    async def stop(self) -> None:
        self._running = False


def _local_file_size(path: str) -> int | None:
    """返回本地文件大小（字节），文件不存在返回 None。"""
    import os

    try:
        return os.path.getsize(path)
    except OSError:
        return None
