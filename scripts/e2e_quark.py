"""真实夸克网盘本地端到端测试脚本。

在本地环境用真实夸克 Cookie + 一个有效分享链接，
验证完整业务链路：inspect（解析分享）→ save（转存）→ download（下载）。

安全约束：
- Cookie 只从环境变量或本地 .env.quark 文件读取，绝不打印、绝不写日志。
- 只下载分享中的第一个非目录文件（小文件），不批量下载。
- 转存到独立子目录，不覆盖账号原有文件。
- 下载到临时目录，完成后打印文件路径与大小。

用法（二选一提供凭据，绝不粘贴到聊天）：

方式 A：环境变量
    export PANBRIDGE_QUARK_COOKIE='<你的Cookie>'
    export PANBRIDGE_SECRET_KEY='<主密钥>'
    export PANBRIDGE_QUARK_SHARE='https://pan.quark.cn/s/xxxx 提取码: xxxx'
    PYTHONPATH=src python scripts/e2e_quark.py

方式 B：本地文件（推荐，脚本会自动读取）
    复制本脚本同目录下的 quark.env.example 为 .env.quark 并填入，
    然后运行：
    PYTHONPATH=src python scripts/e2e_quark.py

注意：本脚本会真实调用夸克接口，转存会真实写入你的网盘。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from panbridge.application.link_extraction import extract_share
from panbridge.application.service import ApplicationService, CreateTaskInput
from panbridge.domain.enums import Action, TaskStatus
from panbridge.domain.models import Account
from panbridge.downloads.http import HttpDownloader
from panbridge.infrastructure.crypto import CredentialCipher
from panbridge.persistence import (
    AccountRepository,
    create_engine,
    create_sessionmaker,
    init_db,
    session_scope,
)
from panbridge.providers.quark.provider import QuarkProvider
from panbridge.providers.registry import ProviderRegistry
from panbridge.workers.worker import Worker


def _load_env_file(path: Path) -> dict[str, str]:
    """读取简单的 KEY=VALUE 文件，返回 dict。不打印任何值。"""
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _resolve_config() -> tuple[str, str, str, str]:
    """解析 Cookie、主密钥、分享链接、可选目标目录 fid。

    优先环境变量，其次 .env.quark。
    """
    env_file = _load_env_file(Path(__file__).resolve().parent.parent / ".env.quark")

    cookie = os.environ.get("PANBRIDGE_QUARK_COOKIE") or env_file.get(
        "PANBRIDGE_QUARK_COOKIE", ""
    )
    secret = os.environ.get("PANBRIDGE_SECRET_KEY") or env_file.get(
        "PANBRIDGE_SECRET_KEY", ""
    )
    share = os.environ.get("PANBRIDGE_QUARK_SHARE") or env_file.get(
        "PANBRIDGE_QUARK_SHARE", ""
    )
    dest_fid = os.environ.get("PANBRIDGE_QUARK_DEST_FID") or env_file.get(
        "PANBRIDGE_QUARK_DEST_FID", ""
    )

    if not cookie:
        raise SystemExit(
            "[配置缺失] 未找到夸克 Cookie。\n"
            "请设置环境变量 PANBRIDGE_QUARK_COOKIE，或创建 .env.quark 文件。"
        )
    if not secret:
        raise SystemExit(
            "[配置缺失] 未找到 PANBRIDGE_SECRET_KEY。\n"
            "生成方式：python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    if not share:
        raise SystemExit(
            "[配置缺失] 未找到分享链接 PANBRIDGE_QUARK_SHARE。\n"
            "请提供一个有效夸克分享链接（含提取码，如："
            "https://pan.quark.cn/s/xxxx 提取码: xxxx）。"
        )
    return cookie, secret, share, dest_fid


async def main() -> None:
    cookie, secret, share_text, dest_fid = _resolve_config()

    # 解析分享链接，得到 provider / share_id / password
    extracted = extract_share(share_text)
    if extracted.provider != "quark":
        raise SystemExit(f"[错误] 提供的不是夸克分享链接：{extracted.provider}")

    cipher = CredentialCipher(secret)
    provider = QuarkProvider(cipher=cipher)

    tmpdir = tempfile.mkdtemp(prefix="panbridge-quark-e2e-")
    engine = create_engine(f"sqlite+aiosqlite:///{tmpdir}/test.db")
    await init_db(engine)
    sessionmaker = create_sessionmaker(engine)

    # 用加密后的 Cookie 建账号
    encrypted = cipher.encrypt(cookie)
    account = Account(
        id="quark-e2e",
        provider="quark",
        label="e2e-test",
        encrypted_credential=encrypted,
    )
    async with session_scope(sessionmaker) as session:
        await AccountRepository(session).add(account)

    registry = ProviderRegistry()
    registry.register(provider)

    download_dir = os.path.join(tmpdir, "dl")
    downloader = HttpDownloader(timeout=60)
    service = ApplicationService(registry, sessionmaker, downloader=downloader)
    worker = Worker(
        registry,
        sessionmaker,
        downloader=downloader,
        download_dir=download_dir,
        poll_interval=0.05,
    )

    worker_task = asyncio.create_task(worker.run())

    try:
        # ---- Step 1: 校验账号 Cookie 是否有效 ----
        ok = await provider.validate_account(account)
        if not ok:
            print("[step1] 账号校验失败：Cookie 无效或已过期")
            return
        print("[step1] 账号 Cookie 校验通过")

        # ---- Step 2: inspect 解析分享 ----
        inspect = await service.inspect_share(share_text)
        print(
            f"[step2] inspect 成功：标题={inspect['title']!r} "
            f"文件数={len(inspect['files'])}"
        )
        files = [f for f in inspect["files"] if not f["is_dir"]]
        if not files:
            print("[step2] 分享中没有可下载的文件")
            return
        # 只取第一个非目录文件（小文件）
        target = files[0]
        print(
            f"[step2] 选择文件：name={target['name']!r} "
            f"size={target.get('size')} file_id={target['file_id']}"
        )

        # ---- Step 3: save_and_download ----
        # 注意：夸克「直接下载分享源文件」需要先转存到自己网盘，
        # 下载必须用转存后的新 fid。因此用 save_and_download 一个动作
        # 完成「转存 → 解析新 fid → 下载」的完整链路。
        #
        # destination 策略：
        # - 不指定（默认）：Worker 会自动创建独立文件夹（panbridge-<task前缀>），
        #   转存到该文件夹，避免污染根目录、避免同名文件冲突。
        # - 指定 PANBRIDGE_QUARK_DEST_FID：使用你提供的已有空文件夹 fid，
        #   不会静默回退到根目录。
        task = await service.create_task(
            CreateTaskInput(
                text=share_text,
                action=Action.SAVE_AND_DOWNLOAD,
                account_id=account.id,
                selection=[target["file_id"]],
                destination=dest_fid or None,  # 空则自动创建独立文件夹
            )
        )
        print(f"[step3] save_and_download 任务已创建：task_id={task.id}")
        if dest_fid:
            print(f"[step3] 使用指定目标目录 fid={dest_fid}")
        else:
            print("[step3] 将自动创建独立文件夹进行转存")

        # ---- 等待任务完成 ----
        result = None
        for _ in range(600):  # 最多等约 60 秒
            await asyncio.sleep(0.1)
            result = await service.get_task(task.id)
            if result.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                break
        print(
            f"[result] status={result.status.value} "
            f"error={result.error_code} msg={result.error_message}"
        )
        if result.status != TaskStatus.COMPLETED:
            print("[result] 任务未完成，终止")
            return

        # ---- 打印下载结果（不含凭据）----
        dl_files = (
            list(Path(download_dir).iterdir())
            if Path(download_dir).exists()
            else []
        )
        for f in dl_files:
            print(f"[download] 文件已保存：{f.name} ({f.stat().st_size} bytes)")
        print("\n✅ 真实夸克端到端链路验证完成")

    finally:
        worker._running = False
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
