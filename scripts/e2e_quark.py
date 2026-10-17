"""真实夸克网盘本地端到端测试脚本（基于扫码登录的账号）。

不再需要 `.env.quark` / Cookie / 主密钥——账号通过扫码登录保存在本地
数据库（`panbridge auth login`），脚本直接读取已登录账号完成
`inspect（解析分享）→ save_and_download（转存+下载）` 全链路验证。

安全约束：
- 不使用任何明文 Cookie，凭据由应用层解密后仅在内存中使用。
- 只下载分享中的第一个非目录文件（小文件），不批量下载。
- 转存到独立子目录，不覆盖账号原有文件。
- 下载到临时目录，完成后打印文件路径与大小。

用法：
    1. 先扫码登录：panbridge auth login
    2. 提供分享链接：
       export PANBRIDGE_QUARK_SHARE='https://pan.quark.cn/s/xxxx 提取码: xxxx'
       uv run python scripts/e2e_quark.py
    （也可通过 --share 参数传入分享链接）
"""

from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
from pathlib import Path

from panbridge.application.auth_service import AuthService
from panbridge.application.link_extraction import extract_share
from panbridge.application.service import ApplicationService, CreateTaskInput
from panbridge.domain.enums import Action, TaskStatus
from panbridge.downloads.http import HttpDownloader
from panbridge.infrastructure.config import Settings
from panbridge.infrastructure.crypto import CredentialCipher
from panbridge.persistence import create_engine, create_sessionmaker, init_db
from panbridge.providers.quark.provider import QuarkProvider
from panbridge.providers.registry import ProviderRegistry
from panbridge.workers.worker import Worker


def _resolve_share(args) -> str:
    share = args.share or os.environ.get("PANBRIDGE_QUARK_SHARE", "")
    if not share:
        raise SystemExit(
            "[配置缺失] 请提供夸克分享链接：\n"
            "  --share 'https://pan.quark.cn/s/xxxx 提取码: xxxx'\n"
            "或设置环境变量 PANBRIDGE_QUARK_SHARE。"
        )
    return share


async def main() -> None:
    parser = argparse.ArgumentParser(description="夸克真实端到端测试")
    parser.add_argument("--share", help="夸克分享链接（含提取码）")
    args = parser.parse_args()
    share_text = _resolve_share(args)

    settings = Settings.from_env()
    engine = create_engine(settings.database_url)
    await init_db(engine)
    sessionmaker = create_sessionmaker(engine)

    cipher = CredentialCipher(settings.secret_key)
    provider = QuarkProvider(cipher=cipher)
    registry = ProviderRegistry()
    registry.register(provider)

    service = ApplicationService(registry, sessionmaker)
    auth = AuthService(service, registry, None)  # 本脚本只查状态，不扫码

    # 读取已登录账号
    status = await auth.login_status("quark")
    account = status.get("account")
    if not status.get("logged_in") or account is None:
        hint = "已失效" if status.get("expired") else "未登录"
        raise SystemExit(
            f"[错误] 夸克账号{hint}，请先扫码登录：panbridge auth login"
        )
    print(f"[账号] 使用已登录账号 id={account['id']}")

    # 解析分享链接
    extracted = extract_share(share_text)
    if extracted.provider != "quark":
        raise SystemExit(f"[错误] 提供的不是夸克分享链接：{extracted.provider}")

    tmpdir = tempfile.mkdtemp(prefix="panbridge-quark-e2e-")
    download_dir = os.path.join(tmpdir, "dl")
    downloader = HttpDownloader(timeout=60)
    worker = Worker(
        registry,
        sessionmaker,
        downloader=downloader,
        download_dir=download_dir,
        poll_interval=0.05,
    )
    worker_task = asyncio.create_task(worker.run())

    try:
        # ---- Step 1: inspect 解析分享 ----
        inspect = await service.inspect_share(share_text)
        print(
            f"[step1] inspect 成功：标题={inspect['title']!r} "
            f"文件数={len(inspect['files'])}"
        )
        files = [f for f in inspect["files"] if not f["is_dir"]]
        if not files:
            print("[step1] 分享中没有可下载的文件")
            return
        target = files[0]
        print(
            f"[step1] 选择文件：name={target['name']!r} "
            f"size={target.get('size')} file_id={target['file_id']}"
        )

        # ---- Step 2: save_and_download ----
        # 夸克下载需先转存，用 save_and_download 完成全链路。
        # 不指定 destination 时 Worker 自动创建独立文件夹，避免污染根目录。
        task = await service.create_task(
            CreateTaskInput(
                text=share_text,
                action=Action.SAVE_AND_DOWNLOAD,
                account_id=account["id"],
                selection=[target["file_id"]],
                destination=None,  # 自动创建独立文件夹
            )
        )
        print(f"[step2] save_and_download 任务已创建：task_id={task.id}")

        # ---- 等待任务完成 ----
        result = None
        for _ in range(600):  # 最多约 60 秒
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
