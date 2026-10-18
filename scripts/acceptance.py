"""批量验收脚本：扫码登录 + 提供分享链接，批量验收各平台。

目标：用户只需两步即可验收平台，无需手工抓 Cookie 或写 curl：
1. 对需账号的平台先扫码登录：`panbridge auth login --provider <平台>`
2. 提供分享链接，脚本自动判断「匿名直链下载」还是「先转存再下载」，
   并验证完整链路。

用法：
    # 匿名可下载的平台（无需账号），抽样验证（下载第一个文件）
    python scripts/acceptance.py --share 'https://www.123pan.com/s/xxxx'
    # 完整目录验证（递归下载所有文件，保留目录结构）
    python scripts/acceptance.py --share 'https://pan.quark.cn/s/xxxx' --mode full
    # 限制最大下载量（超过则跳过整目录验证，仅抽样）
    python scripts/acceptance.py --share '...' --mode full --max-download-bytes 10485760

    # 批量验收：把链接写进一个文本文件（每行一条）
    python scripts/acceptance.py --file links.txt

安全约束：
- 不使用明文 Cookie；凭据由应用层解密后仅在内存中使用。
- 转存到独立子目录，不覆盖账号原有文件。
- 下载到临时目录，完成后打印文件路径与大小。

判断逻辑（核心目标）：
- 平台支持「匿名分享直链下载」（capabilities 含 get_share_download_info）
  → 走 DOWNLOAD（匿名，无需账号）。
- 否则 → 走 SAVE_AND_DOWNLOAD（先转存再下载，需账号）。

验收模式：
- sample（默认）：功能抽样验证——只下载分享中的第一个非目录文件，
  验证「解析 → 直链/转存 → 下载落盘」链路是否打通。通过不代表整目录可用。
- full：完整目录验证——递归下载分享中所有文件，保留目录结构，校验
  每个文件大小。通过才代表该平台多级目录能力完整可用。
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from pathlib import Path

from panbridge.application.assembly import build_registry
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
from panbridge.workers.worker import Worker

_DEFAULT_MAX_BYTES = 50 * 1024 * 1024  # 完整目录验证默认最多下载 50MB


async def _get_account(service, sessionmaker, provider: str):
    """获取该平台最近登录的账号（无则返回 None）。"""
    from panbridge.persistence import AccountRepository

    async with session_scope(sessionmaker) as session:
        return await AccountRepository(session).get_default(provider)


async def _verify_one(
    service,
    worker,
    sessionmaker,
    provider_impl,
    share_text: str,
    tmpdir: str,
    mode: str,
    max_bytes: int,
) -> dict:
    """验收单个分享链接，返回结果摘要。

    mode: "sample" 抽样验证（只下载第一个文件）；"full" 完整目录验证。
    """
    extracted = extract_share(share_text)
    provider = extracted.provider
    result = {
        "provider": provider,
        "share": share_text[:60],
        "ok": False,
        "mode": mode,
    }

    # Step 1: inspect 解析分享
    try:
        share = await provider_impl.inspect_share(
            extracted.share_id, extracted.password
        )
    except Exception as exc:  # noqa: BLE001
        result["stage"] = "inspect"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    result["title"] = share.title
    result["root_items"] = len(share.files)

    # Step 2: 判断下载模式（匿名直链 vs 先转存）
    anon_capable = "get_share_download_info" in provider_impl.capabilities
    if anon_capable:
        action = Action.DOWNLOAD
        account_id = None
    else:
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

    # Step 3: 决定 selection（抽样 vs 完整目录）
    selection: list[str] | None = None
    if mode == "sample":
        # 抽样：只下载第一个非目录文件（递归查找）。
        target = await _find_first_file(
            provider_impl, extracted.share_id, extracted.password, share.files
        )
        if target is None:
            result["stage"] = "inspect"
            result["error"] = "分享中无非目录文件（递归 3 层后仍无）"
            result["note"] = f"解析成功，标题={share.title!r}"
            return result
        selection = [target.file_id]
        result["file"] = target.name
    else:
        # 完整目录：下载前先预估整目录大小，超限则拒绝下载（fail fast），
        # 避免先下载数 GB 再回头校验造成浪费。
        estimated = await _estimate_total_size(
            provider_impl, extracted.share_id, extracted.password, share.files
        )
        result["estimated_bytes"] = estimated
        if estimated is not None and estimated > max_bytes:
            result["stage"] = "precheck"
            result["error"] = (
                f"整目录预估大小 {estimated} 字节超过限制 {max_bytes} 字节，"
                f"已阻止下载；请用 --mode sample 抽样验证或提高 "
                f"--max-download-bytes"
            )
            return result
        # 完整目录：不设 selection，Worker 递归下载所有文件。
        result["file"] = "(整目录)"

    # Step 4: 创建任务
    task = await service.create_task(
        CreateTaskInput(
            text=share_text,
            action=action,
            account_id=account_id,
            selection=selection,
            destination=None,  # 转存时自动建独立文件夹
        )
    )

    # Step 5: 等待完成
    for _ in range(6000):
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

    # Step 6: 检查下载文件落盘
    files = (t.result or {}).get("files") or []
    result["downloaded"] = [
        (f["rel_path"], f.get("size")) for f in files
    ]
    result["ok"] = bool(files)
    result["mode"] = (
        "anonymous_download" if anon_capable else "save_and_download"
    )

    # Step 7: 下载后二次校验（防御性兜底；正常情况已在下载前拦截）。
    # 分享列表 size 为 None 时前置预估会返回「未知」而不阻断，此处
    # 依据实际落盘大小再校验一次，双保险。
    if mode == "full":
        total = sum(sz for _, sz in result["downloaded"] if sz)
        if total > max_bytes:
            result["ok"] = False
            result["error"] = (
                f"整目录下载量 {total} 字节超过限制 {max_bytes} 字节，"
                f"请用 --mode sample 抽样验证"
            )
    return result


async def _find_first_file(provider, share_id, password, files, depth=0):
    """递归查找分享中第一个非目录文件（最多 3 层）。"""
    for f in files:
        if not f.is_dir:
            return f
    if depth >= 3:
        return None
    for f in files:
        if f.is_dir:
            try:
                sub = await provider.list_share_files(
                    share_id, password, f.file_id
                )
            except Exception:  # noqa: BLE001
                continue
            found = await _find_first_file(
                provider, share_id, password, sub, depth + 1
            )
            if found:
                return found
    return None


async def _estimate_total_size(provider, share_id, password, files, depth=0):
    """递归预估分享整目录文件总字节数（用于下载前限额检查）。

    文件 size 为 None 时无法精确统计，返回 None 表示「未知」（调用方
    据此不阻断，交由下载器 max_size 兜底）。超过 5 层深度则停止深挖，
    按当前已知大小返回（避免过度递归）。
    """
    total = 0
    unknown = False
    for f in files:
        if f.is_dir:
            if depth >= 5:
                unknown = True
                continue
            try:
                sub = await provider.list_share_files(
                    share_id, password, f.file_id
                )
            except Exception:  # noqa: BLE001
                unknown = True
                continue
            sub_total = await _estimate_total_size(
                provider, share_id, password, sub, depth + 1
            )
            if sub_total is None:
                unknown = True
            else:
                total += sub_total
        else:
            if f.size is None:
                unknown = True
            else:
                total += f.size
    return None if unknown else total


async def main() -> None:
    parser = argparse.ArgumentParser(description="PanBridge 批量验收")
    parser.add_argument("--share", help="单个分享链接（含提取码）")
    parser.add_argument("--file", help="批量链接文件（每行一条）")
    parser.add_argument(
        "--download-dir", help="下载目录（默认临时目录）", default=None
    )
    parser.add_argument(
        "--mode",
        choices=["sample", "full"],
        default="sample",
        help="验收模式：sample=功能抽样（默认，只下载第一个文件）；"
        "full=完整目录验证（递归下载所有文件并校验）",
    )
    parser.add_argument(
        "--max-download-bytes",
        type=int,
        default=_DEFAULT_MAX_BYTES,
        help=f"完整目录验证的最大下载量（字节，默认 {_DEFAULT_MAX_BYTES}）",
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
    registry = build_registry(cipher)

    download_dir = args.download_dir or tempfile.mkdtemp(prefix="panbridge-accept-")
    # 单文件下载量上限与 --max-download-bytes 对齐：即便整目录预估漏判
    # （size 未知），下载器也会在单文件超过上限时硬性中止，避免下载超大文件。
    downloader = HttpDownloader(
        timeout=60, max_size=args.max_download_bytes
    )
    service = ApplicationService(registry, sessionmaker, downloader=downloader)
    worker = Worker(
        registry, sessionmaker, downloader=downloader, download_dir=download_dir,
        poll_interval=0.05,
    )
    worker_task = asyncio.create_task(worker.run())

    print(f"下载目录: {download_dir}")
    print(f"验收模式: {args.mode}（最大下载量 {args.max_download_bytes} 字节）\n")
    results = []
    try:
        for link in links:
            extracted = extract_share(link)
            provider_impl = registry.get(extracted.provider)
            print(f"=== 验收 [{extracted.provider}] {link[:60]} ===")
            r = await _verify_one(
                service, worker, sessionmaker, provider_impl, link,
                download_dir, args.mode, args.max_download_bytes,
            )
            results.append(r)
            status = "✅" if r["ok"] else "❌"
            print(f"  {status} [{r['mode']}] {r.get('file', '?')}")
            if not r["ok"]:
                print(f"     错误: {r.get('error', '?')}")
            if r.get("downloaded"):
                n = len(r["downloaded"])
                print(f"     已下载 {n} 个文件:")
                for rel, size in r["downloaded"][:10]:
                    print(f"       - {rel} ({size} bytes)")
                if n > 10:
                    print(f"       ... 共 {n} 个文件")
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
        print(f"  {mark} [{r['provider']}] {r['mode']} "
              f"{r.get('file', r.get('error', ''))}")
    ok_count = sum(1 for r in results if r["ok"])
    print(f"\n通过 {ok_count}/{len(results)}（模式 {args.mode}）")


if __name__ == "__main__":
    asyncio.run(main())
