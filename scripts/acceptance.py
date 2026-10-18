"""批量验收脚本：扫码登录 + 提供分享链接，批量验收各平台。

目标：用户只需两步即可验收平台，无需手工抓 Cookie 或写 curl：
1. 对需账号的平台先扫码登录：`panbridge auth login --provider <平台>`
2. 提供分享链接，脚本自动判断「匿名直链下载」还是「先转存再下载」，
   并验证完整链路。

用法：
    # 匿名可下载的平台（无需账号）
    python scripts/acceptance.py --share 'https://www.123pan.com/s/xxxx'
    # 需转存的平台（先扫码登录）
    python scripts/acceptance.py --share 'https://pan.quark.cn/s/xxxx'

    # 批量验收：把链接写进一个文本文件（每行一条）
    python scripts/acceptance.py --file links.txt

安全约束：
- 不使用明文 Cookie；凭据由应用层解密后仅在内存中使用。
- 只下载分享中的第一个非目录文件（小文件），不批量下载。
- 转存到独立子目录，不覆盖账号原有文件。
- 下载到临时目录，完成后打印文件路径与大小。

判断逻辑（核心目标）：
- 平台支持「匿名分享直链下载」（capabilities 含 get_share_download_info）
  → 走 DOWNLOAD（匿名，无需账号）。
- 否则 → 走 SAVE_AND_DOWNLOAD（先转存再下载，需账号）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
from pathlib import Path

from panbridge.application.link_extraction import extract_share
from panbridge.application.service import ApplicationService, CreateTaskInput
from panbridge.domain.enums import Action, TaskStatus
from panbridge.downloads.http import HttpDownloader
from panbridge.infrastructure.config import Settings
from panbridge.infrastructure.crypto import CredentialCipher
from panbridge.persistence import (
    create_engine,
    create_sessionmaker,
    init_db,
    session_scope,
)
from panbridge.providers.registry import ProviderRegistry
from panbridge.workers.worker import Worker


def build_full_registry(cipher: CredentialCipher | None) -> ProviderRegistry:
    """构建包含全部真实 Provider 的注册表（与 main.build_app 一致）。"""
    from panbridge.providers.aliyun.provider import AliyunProvider
    from panbridge.providers.baidu.provider import BaiduProvider
    from panbridge.providers.caiyun.provider import CaiyunProvider
    from panbridge.providers.cloud189.provider import Cloud189Provider
    from panbridge.providers.guangya.provider import GuangyaProvider
    from panbridge.providers.lanzou.provider import LanzouProvider
    from panbridge.providers.pan115.provider import Pan115Provider
    from panbridge.providers.pan123.provider import Pan123Provider
    from panbridge.providers.pikpak.provider import PikPakProvider
    from panbridge.providers.quark.provider import QuarkProvider
    from panbridge.providers.uc.provider import UcProvider
    from panbridge.providers.weiyun.provider import WeiyunProvider
    from panbridge.providers.wukong.provider import WukongProvider
    from panbridge.providers.xunlei.provider import XunleiProvider

    registry = ProviderRegistry()
    registry.register(QuarkProvider(cipher=cipher))
    registry.register(AliyunProvider(cipher=cipher))
    registry.register(LanzouProvider())
    registry.register(Pan123Provider(cipher=cipher))
    registry.register(BaiduProvider(cipher=cipher))
    registry.register(Pan115Provider(cipher=cipher))
    registry.register(PikPakProvider())
    registry.register(Cloud189Provider(cipher=cipher))
    registry.register(CaiyunProvider(cipher=cipher))
    registry.register(UcProvider(cipher=cipher))
    registry.register(XunleiProvider(cipher=cipher))
    registry.register(WeiyunProvider(cipher=cipher))
    registry.register(GuangyaProvider(cipher=cipher))
    registry.register(WukongProvider(cipher=cipher))
    return registry


async def _get_account(service, sessionmaker, provider: str):
    """获取该平台最近登录的账号（无则返回 None）。"""
    from panbridge.persistence import AccountRepository

    async with session_scope(sessionmaker) as session:
        return await AccountRepository(session).get_default(provider)


async def _verify_one(
    service, worker, sessionmaker, provider_impl, share_text: str, tmpdir: str
) -> dict:
    """验收单个分享链接，返回结果摘要。"""
    extracted = extract_share(share_text)
    provider = extracted.provider
    result = {"provider": provider, "share": share_text[:60], "ok": False}

    # Step 1: inspect 解析分享
    try:
        share = await provider_impl.inspect_share(
            extracted.share_id, extracted.password
        )
    except Exception as exc:  # noqa: BLE001
        result["stage"] = "inspect"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    files = [f for f in share.files if not f.is_dir]
    # 根目录全是目录时，递归找第一个非目录文件（最多 3 层）。
    if not files:
        async def _find_first_file(files, depth=0):
            for f in files:
                if not f.is_dir:
                    return f
            if depth >= 3:
                return None
            for f in files:
                if f.is_dir:
                    sub = await provider_impl.list_share_files(
                        extracted.share_id, extracted.password, f.file_id
                    )
                    found = await _find_first_file(sub, depth + 1)
                    if found:
                        return found
            return None

        target = await _find_first_file(share.files)
        if target is None:
            result["stage"] = "inspect"
            result["error"] = "分享中无非目录文件（递归 3 层后仍无）"
            result["note"] = f"解析成功，标题={share.title!r}"
            return result
    else:
        target = files[0]
    result["title"] = share.title
    result["file"] = target.name

    # Step 2: 判断下载模式
    anon_capable = "get_share_download_info" in provider_impl.capabilities
    if anon_capable:
        # 匿名直链下载（无需账号）
        action = Action.DOWNLOAD
        account_id = None
    else:
        # 先转存再下载（需账号）
        action = Action.SAVE_AND_DOWNLOAD
        account = await _get_account(service, sessionmaker, provider)
        if account is None:
            result["stage"] = "download"
            result["error"] = (
                f"平台 {provider} 需登录，请先 `panbridge auth login "
                f"--provider {provider}` 扫码登录"
            )
            return result
        account_id = account.id

    # Step 3: 创建任务
    task = await service.create_task(
        CreateTaskInput(
            text=share_text,
            action=action,
            account_id=account_id,
            selection=[target.file_id],
            destination=None,  # 转存时自动建独立文件夹
        )
    )

    # Step 4: 等待完成
    for _ in range(600):
        await asyncio.sleep(0.1)
        t = await service.get_task(task.id)
        if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            break
    else:
        result["stage"] = "task"
        result["error"] = "任务超时未完成"
        return result

    if t.status != TaskStatus.COMPLETED:
        result["stage"] = "task"
        result["error"] = f"{t.error_code}: {t.error_message}"
        return result

    # Step 5: 检查下载文件落盘
    dl_dir = worker.download_dir
    downloaded = []
    if os.path.isdir(dl_dir):
        downloaded = [
            (f.name, os.path.getsize(os.path.join(dl_dir, f.name)))
            for f in os.listdir(dl_dir)
            if os.path.isfile(os.path.join(dl_dir, f.name))
        ]
    result["ok"] = bool(downloaded)
    result["downloaded"] = downloaded
    result["mode"] = "anonymous_download" if anon_capable else "save_and_download"
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description="PanBridge 批量验收")
    parser.add_argument("--share", help="单个分享链接（含提取码）")
    parser.add_argument("--file", help="批量链接文件（每行一条）")
    parser.add_argument(
        "--download-dir", help="下载目录（默认临时目录）", default=None
    )
    args = parser.parse_args()

    links: list[str] = []
    if args.share:
        links.append(args.share)
    if args.file:
        links.extend(
            line.strip()
            for line in Path(args.file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        )
    if not links:
        raise SystemExit(
            "[配置缺失] 请提供分享链接：--share 或 --file links.txt"
        )

    settings = Settings.from_env()
    engine = create_engine(settings.database_url)
    await init_db(engine)
    sessionmaker = create_sessionmaker(engine)
    cipher = CredentialCipher(settings.secret_key) if settings.secret_key else None
    registry = build_full_registry(cipher)

    download_dir = args.download_dir or tempfile.mkdtemp(prefix="panbridge-accept-")
    downloader = HttpDownloader(timeout=60, max_size=10 * 1024 * 1024)
    service = ApplicationService(registry, sessionmaker, downloader=downloader)
    worker = Worker(
        registry, sessionmaker, downloader=downloader, download_dir=download_dir,
        poll_interval=0.05,
    )
    worker_task = asyncio.create_task(worker.run())

    print(f"下载目录: {download_dir}\n")
    results = []
    try:
        for link in links:
            extracted = extract_share(link)
            provider_impl = registry.get(extracted.provider)
            print(f"=== 验收 [{extracted.provider}] {link[:60]} ===")
            r = await _verify_one(
                service, worker, sessionmaker, provider_impl, link, download_dir
            )
            results.append(r)
            status = "✅" if r["ok"] else "❌"
            print(f"  {status} {r.get('mode', '?')} | {r.get('file', '?')}")
            if not r["ok"]:
                print(f"     错误: {r.get('error', '?')}")
            if r.get("downloaded"):
                for name, size in r["downloaded"]:
                    print(f"     已下载: {name} ({size} bytes)")
            print()
    finally:
        worker._running = False
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        await engine.dispose()

    # 汇总
    print("=" * 60)
    print("验收结果汇总：")
    for r in results:
        mark = "✅" if r["ok"] else "❌"
        print(f"  {mark} [{r['provider']}] {r.get('mode', '?')} "
              f"{r.get('file', r.get('error', ''))}")
    ok_count = sum(1 for r in results if r["ok"])
    print(f"\n通过 {ok_count}/{len(results)}")


if __name__ == "__main__":
    asyncio.run(main())
