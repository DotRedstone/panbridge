"""panbridge 命令行入口。

提供扫码登录、登录状态查询、退出登录三个子命令，无需 WebUI。

用法：
    panbridge auth login     # 终端显示二维码，夸克 App 扫码登录
    panbridge auth status    # 查询当前登录状态
    panbridge auth logout    # 退出登录（删除本地账号）
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from panbridge.application.assembly import (
    build_auth_service,
    build_registry,
)
from panbridge.application.service import ApplicationService
from panbridge.infrastructure.config import Settings
from panbridge.infrastructure.crypto import CredentialCipher
from panbridge.persistence import (
    create_engine,
    create_sessionmaker,
    init_db,
)

logging.basicConfig(level=logging.WARNING)


def _display_qr(qr_url: str) -> None:
    """在终端渲染 ASCII 二维码（无外部服务、无浏览器自动化）。"""
    print("\n请使用「夸克」App 扫描下方二维码登录：\n")
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(qr_url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception:  # noqa: BLE001
        # 二维码渲染失败时，给出可手动打开的链接（不包含凭据）
        print(f"二维码渲染失败，请在浏览器中打开：{qr_url}")
    print("\n（也可在浏览器打开登录链接）\n")


def _build(settings: Settings):
    engine = create_engine(settings.database_url)
    sessionmaker = create_sessionmaker(engine)
    cipher = CredentialCipher(settings.secret_key)
    registry = build_registry(cipher)
    service = ApplicationService(registry, sessionmaker)
    auth = build_auth_service(service, registry, sessionmaker, cipher)
    return engine, auth


async def _login(provider: str) -> int:
    settings = Settings.from_env()
    engine, auth = _build(settings)
    await init_db(engine)
    try:
        info = await auth.start_qr_login(provider)
        session_id = info["session_id"]
        _display_qr(info["qr_url"])

        print("等待扫码登录中（最多 5 分钟）...\n")
        for _ in range(150):  # 每 2 秒轮询一次
            await asyncio.sleep(2)
            status = await auth.check_qr_login(session_id)
            if status.get("status") == "success":
                result = await auth.finish_qr_login(session_id)
                print(
                    "✅ 登录成功！账号已保存。\n"
                    f"   account_id = {result['account_id']}\n"
                    f"   provider   = {result['provider']}"
                )
                return 0
            if status.get("status") == "expired":
                print("⏰ 二维码已过期，请重新运行 `panbridge auth login`")
                return 1

        print("⏰ 登录超时，请重新运行 `panbridge auth login`")
        return 1
    finally:
        await engine.dispose()


async def _status(provider: str) -> int:
    settings = Settings.from_env()
    engine, auth = _build(settings)
    await init_db(engine)
    try:
        status = await auth.login_status(provider)
        if status.get("logged_in"):
            account = status.get("account") or {}
            print("✅ 已登录")
            print(f"   account_id = {account.get('id')}")
            print(f"   label      = {account.get('label')}")
            print(f"   provider   = {account.get('provider')}")
        elif status.get("expired"):
            print("⚠️  已保存账号，但凭据已失效，请重新扫码登录：")
            print("   panbridge auth login")
        else:
            print("ℹ️  未登录")
            print("   请运行 `panbridge auth login` 扫码登录")
        return 0
    finally:
        await engine.dispose()


async def _logout(provider: str) -> int:
    settings = Settings.from_env()
    engine, auth = _build(settings)
    await init_db(engine)
    try:
        result = await auth.logout(provider)
        print(f"✅ 已退出登录（移除 {result['removed']} 个账号）")
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="panbridge",
        description="PanBridge 多网盘转存下载后端 CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    auth_parser = sub.add_parser("auth", help="账号认证管理")
    auth_sub = auth_parser.add_subparsers(dest="auth_command", required=True)

    login_p = auth_sub.add_parser("login", help="扫码登录")
    login_p.add_argument("--provider", default="quark", help="网盘平台")

    status_p = auth_sub.add_parser("status", help="查询登录状态")
    status_p.add_argument("--provider", default="quark", help="网盘平台")

    logout_p = auth_sub.add_parser("logout", help="退出登录")
    logout_p.add_argument("--provider", default="quark", help="网盘平台")

    args = parser.parse_args(argv)

    if args.auth_command == "login":
        return asyncio.run(_login(args.provider))
    if args.auth_command == "status":
        return asyncio.run(_status(args.provider))
    if args.auth_command == "logout":
        return asyncio.run(_logout(args.provider))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
