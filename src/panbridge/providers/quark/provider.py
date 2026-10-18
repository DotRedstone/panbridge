"""夸克网盘真实 Adapter。

基于 QuarkPan（MIT）薄封装。QuarkPan 提供同步 API，
本 Adapter 通过 asyncio.to_thread 在线程池中调用，避免阻塞事件循环。

依赖：`quarkpan`（PyPI，MIT License）
- QuarkClient(cookies=...) 用已有 Cookie 初始化，无需二维码登录
- parse_share_url / get_share_token / get_share_info 解析分享
- save_shared_files 转存
- get_download_url 获取下载链接
- list_files 列出账号文件

字段映射（夸克 -> 领域）：
- fid -> file_id
- file_name -> name
- dir (bool) -> is_dir
- size -> size
"""

from __future__ import annotations

import asyncio
from typing import Any

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.domain.models import (
    Account,
    Share,
    ShareFile,
    TransferResult,
)
from panbridge.infrastructure.crypto import CredentialCipher
from panbridge.providers.base import ShareProvider

try:  # pragma: no cover - 依赖可选，避免未安装时导入失败
    from quark_client import QuarkClient, ShareLinkError
    from quark_client.exceptions import (
        APIError as QuarkAPIError,
    )
    from quark_client.exceptions import (
        AuthenticationError,
    )
except ImportError:  # pragma: no cover
    QuarkClient = None  # type: ignore
    ShareLinkError = Exception  # type: ignore
    QuarkAPIError = Exception  # type: ignore
    AuthenticationError = Exception  # type: ignore


class QuarkProvider(ShareProvider):
    name = "quark"
    display_name = "夸克网盘"
    capabilities = frozenset(
        {
            "inspect_share",
            "list_share_files",
            "save_shared_files",
            "list_account_files",
            "get_download_info",
            "validate_account",
            "create_folder",
        }
    )

    def __init__(self, cipher: CredentialCipher | None = None) -> None:
        if QuarkClient is None:  # pragma: no cover
            raise RuntimeError(
                "quarkpan 未安装，无法使用 QuarkProvider。"
                "请安装：pip install quarkpan"
            )
        self._cipher = cipher

    # ---- 内部工具 ----

    def _decrypt_credential(self, account: Account) -> str:
        """把账号的密文凭据解密为明文 Cookie。

        `encrypted_credential` 存的是 Fernet 密文。没有 cipher 时抛错，
        绝不把密文当明文使用。
        """
        token = account.encrypted_credential or ""
        if not token:
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "quark account has no credential (cookie)",
            )
        if self._cipher is None:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "credential cipher not configured; cannot decrypt cookie",
                retryable=False,
            )
        try:
            return self._cipher.decrypt(token)
        except Exception as exc:  # noqa: BLE001
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "quark credential could not be decrypted (secret key mismatch?)",
                retryable=False,
            ) from exc

    def _client_for(self, account: Account) -> Any:
        """用账号的 Cookie 凭据构建同步 QuarkClient。"""
        cookie = self._decrypt_credential(account)
        return QuarkClient(cookies=cookie, auto_login=False)

    async def _run(self, account: Account, func, *args, **kwargs):
        """在线程池中执行同步 QuarkPan 调用，避免阻塞事件循环。"""
        return await asyncio.to_thread(func, *args, **kwargs)

    @staticmethod
    def _map_file(raw: dict[str, Any]) -> ShareFile:
        return ShareFile(
            file_id=str(raw.get("fid", "")),
            name=raw.get("file_name", ""),
            is_dir=bool(raw.get("dir", False)),
            size=raw.get("size"),
            parent_id=raw.get("pdir_fid"),
        )

    @staticmethod
    def _map_quark_error(exc: Exception) -> DomainError:
        """将 QuarkPan 异常映射为统一领域错误。"""
        if isinstance(exc, AuthenticationError):
            return DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "quark account credential is invalid or expired",
                retryable=False,
            )
        if isinstance(exc, ShareLinkError):
            return DomainError(
                ErrorCode.INVALID_SHARE,
                "invalid quark share link",
            )
        if isinstance(exc, QuarkAPIError):
            msg = str(exc)
            # 404 或「分享不存在/失效」→ INVALID_SHARE
            if (
                "404" in msg
                or "不存在" in msg
                or "失效" in msg
                or "已删除" in msg
                or "已取消" in msg
            ):
                return DomainError(
                    ErrorCode.INVALID_SHARE,
                    "quark share does not exist or has expired",
                )
            if "验证码" in msg or "风控" in msg or "captcha" in msg.lower():
                return DomainError(
                    ErrorCode.ACTION_REQUIRED,
                    "quark requires action (captcha/risk control)",
                    retryable=False,
                )
            if "空间" in msg or "quota" in msg.lower() or "容量" in msg:
                return DomainError(
                    ErrorCode.QUOTA_EXCEEDED,
                    "quark quota exceeded",
                    retryable=False,
                )
            if "提取码" in msg or "密码" in msg or "passcode" in msg.lower():
                return DomainError(
                    ErrorCode.INVALID_PASSWORD,
                    "quark extraction password incorrect",
                )
            return DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"quark api error: {msg[:200]}",
                retryable=True,
            )
        return DomainError(
            ErrorCode.INTERNAL_ERROR,
            f"quark error: {exc.__class__.__name__}",
            retryable=True,
        )

    # ---- Provider 接口 ----

    async def inspect_share(self, share_id: str, password: str | None) -> Share:
        # 解析分享不需要登录，用空客户端获取 token 和详情
        try:
            client = QuarkClient(cookies="", auto_login=False)
            token = await self._run(
                client, lambda: client.shares.get_share_token(share_id, password)
            )
            info = await self._run(
                client,
                lambda: client.shares.get_share_info(share_id, token),
            )
        except Exception as exc:  # noqa: BLE001
            raise self._map_quark_error(exc) from exc

        files = self._extract_files(info)
        title = self._extract_title(info, share_id)
        return Share(
            provider=self.name,
            share_id=share_id,
            title=title,
            files=files,
            password=password,
            raw=info if isinstance(info, dict) else {},
        )

    async def list_share_files(
        self, share_id: str, password: str | None, parent_id: str | None = None
    ) -> list[ShareFile]:
        share = await self.inspect_share(share_id, password)
        return share.files

    async def validate_account(self, account: Account) -> bool:
        """校验账号 Cookie 是否有效。

        注意：QuarkPan 的 `client.is_logged_in()` 检查的是本地
        `cookies.json` 文件，与账号存储的 Cookie 无关，故不能用它。
        这里用账号 Cookie 实际调用 `get_storage_info`（capacity 接口），
        能成功返回即说明 Cookie 有效。
        """
        try:
            client = self._client_for(account)
            info = await self._run(client, client.get_storage_info)
            return isinstance(info, dict) and "error" not in info
        except DomainError:
            raise
        except Exception:  # noqa: BLE001
            return False

    async def save_shared_files(
        self,
        account: Account,
        share_id: str,
        password: str | None,
        file_ids: list[str],
        destination: str | None,
        on_conflict: str,
    ) -> TransferResult:
        client = self._client_for(account)
        try:
            token = await self._run(
                client, lambda: client.shares.get_share_token(share_id, password)
            )
            result = await self._run(
                client,
                lambda: client.shares.save_shared_files(
                    share_id=share_id,
                    token=token,
                    file_ids=file_ids,
                    target_folder_id=destination or "0",
                    save_all=False,
                    wait_for_completion=True,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            raise self._map_quark_error(exc) from exc

        task_id = (
            result.get("data", {}).get("task_id")
            if isinstance(result, dict)
            else None
        )
        return TransferResult(
            # 转存前的源文件 ID 不能直接用于下载，需查询转存后的新 ID。
            saved_file_ids=[],
            remote_task_id=str(task_id) if task_id else None,
        )

    async def resolve_saved_file_ids(
        self, account: Account, destination: str | None, names: list[str]
    ) -> list[str]:
        """转存完成后，从目标目录按文件名查询转存后的新文件 ID。

        夸克转存是异步任务，完成后文件出现在目标目录，
        具有新的 fid（与分享源 fid 不同）。下载必须用新 fid。

        同名误匹配防护：若目标目录中某个源文件名匹配到多个文件，
        无法确定唯一新 fid，则抛出错误而非静默选错（避免下错文件）。
        """
        client = self._client_for(account)
        try:
            result = await self._run(
                client, lambda: client.list_files(destination or "0")
            )
        except Exception as exc:  # noqa: BLE001
            raise self._map_quark_error(exc) from exc

        # list_files 返回原始 dict，需先转换为 ShareFile
        files = self._extract_files(result)
        name_set = set(names)
        by_name: dict[str, list[str]] = {}
        for item in files:
            if item.name in name_set and not item.is_dir:
                by_name.setdefault(item.name, []).append(item.file_id)

        resolved: list[str] = []
        for name in names:
            candidates = by_name.get(name, [])
            if len(candidates) == 1:
                resolved.append(candidates[0])
            elif len(candidates) > 1:
                # 同名文件多个，无法确定哪个是刚转存的
                raise DomainError(
                    ErrorCode.INTERNAL_ERROR,
                    f"ambiguous file name after save: {name!r} matched "
                    f"{len(candidates)} files; use a dedicated empty folder",
                    retryable=False,
                )
            # len(candidates) == 0：该文件未转存成功，跳过
        return resolved

    async def await_transfer(
        self, account: Account, remote_task_id: str
    ) -> TransferResult:
        """等待夸克异步转存完成，返回按文件名解析出的新 fid 映射。

        夸克的 save_shared_files 已用 wait_for_completion=True，多数情况下
        任务已同步完成；此处仍提供完成确认语义：列出目标目录确认文件已
        出现。由于夸克不提供「源 fid → 新 fid」的显式映射，只能按文件名
        在唯一目标目录内解析，作为映射兜底（不伪造顺序对应）。
        """
        # 无法从 remote_task_id 反查目标目录，故以 root("0") 为默认；
        # 实际转存时 Worker 传入独立目录 destination，此方法仅在
        # 无法拿到显式映射时被调用，作为「任务已可确认」的信号。
        client = self._client_for(account)
        try:
            await self._run(client, lambda: client.list_files("0"))
        except Exception as exc:  # noqa: BLE001
            raise self._map_quark_error(exc) from exc

        # 不在此处伪造「源→新」映射：夸克无法从任务 ID 反查对应关系，
        # 仅返回空的显式映射，交由 Worker 走 resolve_saved_file_ids 兜底。
        return TransferResult(saved=[], remote_task_id=remote_task_id)

    async def create_folder(
        self, account: Account, name: str, parent_id: str = "0"
    ) -> str:
        """在账号网盘创建目录，返回新目录 fid。

        用于转存到独立目录，避免污染根目录、避免同名文件冲突。
        """
        client = self._client_for(account)
        try:
            result = await self._run(
                client, lambda: client.create_folder(name, parent_id)
            )
        except Exception as exc:  # noqa: BLE001
            raise self._map_quark_error(exc) from exc

        if not isinstance(result, dict) or result.get("status") != 200:
            msg = (
                result.get("message", "未知错误")
                if isinstance(result, dict)
                else "无响应"
            )
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"quark create_folder failed: {msg}",
                retryable=False,
            )
        data = result.get("data")
        fid = data.get("fid") if isinstance(data, dict) else None
        if not fid:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "quark create_folder returned no fid",
                retryable=False,
            )
        return str(fid)

    async def list_account_files(
        self, account: Account, parent_id: str | None = None
    ) -> list[ShareFile]:
        client = self._client_for(account)
        try:
            result = await self._run(
                client, lambda: client.list_files(parent_id or "0")
            )
        except Exception as exc:  # noqa: BLE001
            raise self._map_quark_error(exc) from exc

        files = self._extract_files(result)
        return files

    async def get_download_info(
        self, account: Account, file_id: str
    ) -> dict[str, Any]:
        client = self._client_for(account)
        try:
            url = await self._run(
                client, lambda: client.get_download_url(file_id)
            )
        except Exception as exc:  # noqa: BLE001
            raise self._map_quark_error(exc) from exc

        if not url:
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                "quark returned empty download url",
                retryable=True,
            )
        # 尝试获取文件名（可选，失败不影响下载）。
        # QuarkPan 的 get_file_info 返回单个文件 dict（含 file_name），
        # 而非 {"data": {"list": [...]}} 结构。
        name = f"{file_id}.bin"
        try:
            info = await self._run(
                client, lambda: client.get_file_info(file_id)
            )
            if isinstance(info, dict) and info.get("file_name"):
                name = info["file_name"]
        except Exception:  # noqa: BLE001
            pass

        return {
            "url": url,
            "name": name,
            "size": None,
            "checksum": None,
            # 夸克 CDN 下载可能校验 Referer（QuarkPan 自带下载也带此头）。
            # 注意：不含 Cookie——下载 URL 已带签名，且凭据不得外泄。
            "headers": {
                "Referer": "https://pan.quark.cn/",
                "User-Agent": (
                    "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/139.0.0.0 Mobile Safari/537.36"
                ),
            },
        }

    # ---- 解析辅助 ----

    @staticmethod
    def _extract_files(info: dict[str, Any] | None) -> list[ShareFile]:
        if not isinstance(info, dict):
            return []
        data = info.get("data", {})
        if isinstance(data, dict):
            items = data.get("list", [])
        else:
            items = []
        result: list[ShareFile] = []
        for item in items:
            if isinstance(item, dict) and item.get("fid"):
                result.append(QuarkProvider._map_file(item))
        return result

    @staticmethod
    def _extract_title(info: dict[str, Any] | None, share_id: str) -> str:
        if not isinstance(info, dict):
            return f"quark-share-{share_id}"
        data = info.get("data", {})
        if isinstance(data, dict):
            share = data.get("share", {}) or {}
            if isinstance(share, dict) and share.get("title"):
                return share["title"]
        return f"quark-share-{share_id}"
