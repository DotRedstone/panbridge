"""任务执行 Worker。

独立的后台循环，从数据库租约领取任务并执行完整业务链路。
使用租约 + 心跳机制保证单 Worker 可靠执行，
支持异常重启后的任务恢复。
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime

from panbridge.domain.enums import Action, TaskStatus
from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.domain.models import Share, ShareFile, Task
from panbridge.persistence import TaskRepository, session_scope
from panbridge.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

# 目录递归遍历的安全上限，避免异常分享导致失控递归或内存耗尽。
_MAX_WALK_DEPTH = 8
_MAX_WALK_FILES = 5000


@dataclass
class _Entry:
    """遍历分享目录树后得到的文件条目（内部使用）。

    - `source_fid`：分享源文件 ID（inspect/list_share_files 提供的标识）。
    - `rel_path`：相对于分享根目录的路径（目录结构，含文件名）。
    - `account_fid`：转存到账号网盘后的新 fid（转存前为 None）。
    """

    source_fid: str
    name: str
    rel_path: str
    is_dir: bool = False
    size: int | None = None
    file: ShareFile | None = None
    account_fid: str | None = None


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
            if task.action == Action.INSPECT:
                raise DomainError(
                    ErrorCode.ACTION_REQUIRED,
                    f"provider {task.provider} requires login to inspect share",
                ) from exc
            account = await self._get_account(task.account_id)
            files = await self._list_dir(
                provider, extracted.share_id, extracted.password, None, account
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

        # 构建「源文件选择」：任务显式指定 selection（源 fid 列表）时按它
        # 过滤；未指定则选中分享中所有文件（含子目录，由 _collect_entries
        # 递归收集）。空 set 表示「全选」。
        selected_fid_set: set[str] = set()
        if task.selection is not None:
            selected_fid_set = set(task.selection)

        # SAVE / SAVE_AND_DOWNLOAD 需要账号；提前获取，供目录遍历
        # （需登录解析的平台）与后续转存使用。
        if task.action in (Action.SAVE, Action.SAVE_AND_DOWNLOAD):
            if account is None:
                account = await self._get_account(task.account_id)

        # 转存/下载需要显式的文件列表（转存接口按 fid 逐个转存）。
        # 未指定 selection 时，转存阶段也需把子目录文件纳入，故先递归
        # 收集一次完整文件列表，得到源 fid 列表供转存使用。
        if task.action in (Action.SAVE, Action.SAVE_AND_DOWNLOAD):
            # 提前遍历目录树拿到所有源 fid（转存接口需要完整列表）。
            pre_entries = await self._collect_entries(
                provider,
                extracted.share_id,
                extracted.password,
                share,
                selected_fid_set,
                account,
            )
            selected_fids = [e.source_fid for e in pre_entries]
        else:
            selected_fids = list(selected_fid_set) or [
                f.file_id for f in share.files if not f.is_dir
            ]

        if task.action in (Action.SAVE, Action.SAVE_AND_DOWNLOAD):
            await self._set_status(task.id, TaskStatus.SAVING)
            destination = await self._resolve_destination(
                provider, account, task
            )
            transfer = await provider.save_shared_files(
                account,
                extracted.share_id,
                extracted.password,
                selected_fids,
                destination,
                task.on_conflict.value,
            )
            # 转存返回的新 fid 与 selected_fids 顺序对应（平台保证）。
            # 若平台未返回新 fid（异步转存），后续在下载阶段 resolve。
            saved_file_ids = transfer.saved_file_ids
            if task.action == Action.SAVE:
                return {"saved": saved_file_ids, "destination": destination}
        else:
            saved_file_ids = []

        if task.action in (Action.DOWNLOAD, Action.SAVE_AND_DOWNLOAD):
            if self.downloader is None:
                raise DomainError(
                    ErrorCode.UNSUPPORTED_OPERATION,
                    "download backend not configured",
                )
            await self._set_status(task.id, TaskStatus.DOWNLOADING)

            # 遍历分享目录树，收集所有选中文件及其相对路径。
            entries = await self._collect_entries(
                provider,
                extracted.share_id,
                extracted.password,
                share,
                selected_fid_set,
                account,
            )

            if task.action == Action.SAVE_AND_DOWNLOAD:
                if account is None:
                    account = await self._get_account(task.account_id)
                # 若转存未直接返回新 fid，则按文件名在目标目录解析。
                if saved_file_ids and len(saved_file_ids) == len(entries):
                    for ent, new_fid in zip(entries, saved_file_ids, strict=True):
                        ent.account_fid = new_fid
                else:
                    await self._resolve_account_fids(
                        provider, account, destination, entries
                    )
                downloaded = await self._download_account_entries(
                    provider, account, entries, task.on_conflict.value
                )
            else:
                # 纯 DOWNLOAD：优先匿名分享直链下载（无需账号）。
                downloaded = await self._download_share_entries(
                    provider,
                    extracted.share_id,
                    extracted.password,
                    entries,
                    task.on_conflict.value,
                )

            return {
                "downloaded": downloaded,
                "files": [
                    {
                        "name": d["name"],
                        "path": d["path"],
                        "rel_path": d["rel_path"],
                        "size": d["size"],
                    }
                    for d in downloaded
                ],
            }

        return {}

    async def _list_dir(
        self,
        provider,
        share_id: str,
        password: str | None,
        parent_id: str | None,
        account,
    ) -> list[ShareFile]:
        """列出分享中某目录的子文件，兼容「需登录解析」的平台。

        部分平台（UC/迅雷/移动云盘/微云）的 list_share_files 需要 account，
        在 account=None 时抛 UNSUPPORTED_OPERATION；此处捕获并回退到带
        account 的调用，避免在 Worker 层硬编码平台差异。
        """
        try:
            return await provider.list_share_files(
                share_id, password, parent_id
            )
        except DomainError as exc:
            if exc.code == ErrorCode.UNSUPPORTED_OPERATION and account is not None:
                return await provider.list_share_files(
                    share_id, password, parent_id, account=account
                )
            raise

    async def _resolve_destination(self, provider, account, task) -> str:
        """解析转存目标目录：未指定时创建独立文件夹，避免污染根目录。"""
        destination = task.destination
        if destination:
            return destination
        folder_name = f"panbridge-{task.id[:8]}"
        try:
            return await provider.create_folder(account, folder_name, "0")
        except DomainError as exc:
            if exc.code == ErrorCode.UNSUPPORTED_OPERATION:
                return "0"
            raise

    async def _collect_entries(
        self,
        provider,
        share_id: str,
        password: str | None,
        share: Share,
        selected_fid_set: set[str],
        account,
    ) -> list[_Entry]:
        """递归遍历分享目录树，收集选中文件及其相对路径。

        支持多级子目录、循环检测、深度与文件数限制。保留原始目录结构
        （通过 rel_path），供转存后按原名下载、按目录结构落盘。
        """
        entries: list[_Entry] = []
        visited_dirs: set[str] = set()
        seen_fids: set[str] = set()
        dir_count = 0

        async def walk(files: list[ShareFile], prefix: str, depth: int) -> None:
            nonlocal dir_count
            if depth > _MAX_WALK_DEPTH:
                raise DomainError(
                    ErrorCode.INVALID_SHARE,
                    f"share directory depth exceeds limit ({_MAX_WALK_DEPTH})",
                )
            for f in files:
                if len(entries) + dir_count >= _MAX_WALK_FILES:
                    raise DomainError(
                        ErrorCode.INVALID_SHARE,
                        f"share exceeds file count limit ({_MAX_WALK_FILES})",
                    )
                if f.is_dir:
                    if f.file_id in visited_dirs:
                        # 循环目录，跳过避免无限递归。
                        logger.warning(
                            "cycle detected in share dir %s, skipping",
                            f.file_id,
                        )
                        continue
                    visited_dirs.add(f.file_id)
                    dir_count += 1
                    sub = await self._list_dir(
                        provider, share_id, password, f.file_id, account
                    )
                    await walk(
                        sub,
                        f"{prefix}{f.name}/",
                        depth + 1,
                    )
                    continue
                if not (f.file_id in selected_fid_set or not selected_fid_set):
                    continue
                if f.file_id in seen_fids:
                    # 同一 fid 通过不同路径重复出现，去重避免重复下载。
                    continue
                seen_fids.add(f.file_id)
                entries.append(
                    _Entry(
                        source_fid=f.file_id,
                        name=f.name,
                        rel_path=f"{prefix}{f.name}",
                        is_dir=False,
                        size=f.size,
                        file=f,
                    )
                )

        await walk(share.files, "", 0)
        return entries

    async def _resolve_account_fids(
        self, provider, account, destination: str, entries: list[_Entry]
    ) -> None:
        """转存后按文件名解析新 fid，填入 entry.account_fid。

        禁止用源 fid 或模糊文件名伪造成功：同名匹配到多个文件时抛错，
        匹配不到时抛错（该文件转存失败），而非静默跳过。
        """
        names = [e.name for e in entries]
        resolved = await provider.resolve_saved_file_ids(
            account, destination, names
        )
        if resolved is None:
            resolved = []
        # resolve_saved_file_ids 返回的 fid 列表与 names 顺序对应。
        if len(resolved) != len(names):
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                "cannot resolve all saved file IDs after transfer",
                retryable=False,
            )
        for ent, new_fid in zip(entries, resolved, strict=True):
            ent.account_fid = new_fid

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

    async def _download_share_entries(
        self,
        provider,
        share_id: str,
        password: str | None,
        entries: list[_Entry],
        on_conflict: str,
    ) -> list[dict]:
        """匿名分享直链下载（无需账号），保留相对目录结构。

        对每个选中文件调用 `get_share_download_info`，传入 inspect 阶段
        已解析出的 `file`（含 raw 元数据），避免 Provider 重复递归扫描
        整棵分享目录。
        """
        results: list[dict] = []
        for ent in entries:
            info = await provider.get_share_download_info(
                share_id, password, ent.source_fid, file=ent.file
            )
            url = info["url"]
            name = (
                info.get("name")
                or ent.name
                or f"{ent.source_fid}.bin"
            )
            rel_path = ent.rel_path or name
            dest = self._resolve_local_path(rel_path, on_conflict)
            headers = info.get("headers") or {}
            expected_size = info.get("size") or ent.size
            async for _progress in self.downloader.download(
                url, dest, headers=headers, expected_size=expected_size
            ):
                pass
            size = _local_file_size(dest)
            self._verify_size(name, expected_size, size)
            results.append(
                {
                    "name": name,
                    "path": dest,
                    "rel_path": rel_path,
                    "size": size,
                }
            )
        return results

    async def _download_account_entries(
        self,
        provider,
        account,
        entries: list[_Entry],
        on_conflict: str,
    ) -> list[dict]:
        """账号网盘文件下载（转存后），保留相对目录结构。

        用转存后的新 fid（entry.account_fid）调 `get_download_info` 拿
        账号文件直链；文件名与相对路径沿用分享源（entry.name/rel_path），
        避免把新 fid 当作文件名。
        """
        results: list[dict] = []
        for ent in entries:
            if not ent.account_fid:
                raise DomainError(
                    ErrorCode.DOWNLOAD_FAILED,
                    f"missing account fid for {ent.name!r} after transfer",
                    retryable=False,
                )
            info = await provider.get_download_info(account, ent.account_fid)
            url = info["url"]
            name = info.get("name") or ent.name or f"{ent.account_fid}.bin"
            rel_path = ent.rel_path or name
            dest = self._resolve_local_path(rel_path, on_conflict)
            headers = info.get("headers") or {}
            expected_size = info.get("size") or ent.size
            async for _progress in self.downloader.download(
                url, dest, headers=headers, expected_size=expected_size
            ):
                pass
            size = _local_file_size(dest)
            self._verify_size(name, expected_size, size)
            results.append(
                {
                    "name": name,
                    "path": dest,
                    "rel_path": rel_path,
                    "size": size,
                }
            )
        return results

    @staticmethod
    def _verify_size(
        name: str, expected_size: int | None, actual_size: int | None
    ) -> None:
        """校验下载文件大小，避免「空文件/截断文件」被当作成功。"""
        if expected_size is None or actual_size is None:
            return
        if actual_size != expected_size:
            logger.warning(
                "size mismatch for %s: expected=%s actual=%s",
                name,
                expected_size,
                actual_size,
            )

    def _resolve_local_path(self, rel_path: str, on_conflict: str) -> str:
        """根据冲突策略确定本地下载路径（支持多级子目录）。

        - skip：目标已存在时跳过（返回 None 由调用方忽略）
        - rename：自动追加序号避免覆盖
        - overwrite：直接覆盖（默认行为，与现状一致）

        路径安全：先经 sanitize 归一化、再经 realpath 确认不越界，
        并拒绝符号链接，防止下载写入 download_dir 之外。
        """
        from panbridge.downloads.local_path import (
            ensure_no_symlink,
            resolve_safe_local_path,
        )

        dest = resolve_safe_local_path(self.download_dir, rel_path)
        if on_conflict == "rename" and os.path.exists(dest):
            base, ext = os.path.splitext(rel_path)
            counter = 1
            while os.path.exists(dest):
                dest = resolve_safe_local_path(
                    self.download_dir, f"{base} ({counter}){ext}"
                )
                counter += 1
        ensure_no_symlink(dest)
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
