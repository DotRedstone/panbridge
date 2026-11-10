#!/usr/bin/env python3
"""PanBridge dsh skill CLI —— 调用 PanBridge HTTP API 的零依赖客户端。

供 dsh agent（或人工）通过命令行操作 PanBridge 后端：
解析分享 / 转存 / 下载 / 查任务 / 管理账号 / 扫码登录。

设计约束：
- 纯标准库（urllib + argparse + json + os + sys），零第三方依赖，
  不要求运行环境装 httpx。
- 所有输出为 JSON（stdout），便于 agent 解析；错误也以 JSON 打印到
  stderr 并返回非零退出码。
- 服务端接口契约见 ../docs/api.md 与 src/panbridge/api/router.py。

配置优先级（base_url / api_key 相同）：
  1. 命令行参数 --base-url / --api-key
  2. 环境变量 PANBRIDGE_BASE_URL / PANBRIDGE_API_KEY
  3. 文件 ~/.dsh/panbridge/api_key（api_key 专用，建议 chmod 600）
  4. 默认 http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
CONFIG_DIR = Path.home() / ".dsh" / "panbridge"
API_KEY_FILE = CONFIG_DIR / "api_key"

# 与后端 domain/enums.py 的 Action 保持一致
ACTIONS = ("inspect", "save", "download", "save_and_download")
ON_CONFLICT = ("skip", "rename", "overwrite")


class PanBridgeError(Exception):
    """PanBridge 业务/连接错误。"""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _load_api_key_file() -> str:
    try:
        return API_KEY_FILE.read_text().strip()
    except OSError:
        return ""


def _resolve_base_url(args: argparse.Namespace) -> str:
    return (
        getattr(args, "base_url", None)
        or os.environ.get("PANBRIDGE_BASE_URL")
        or DEFAULT_BASE_URL
    )


def _resolve_api_key(args: argparse.Namespace) -> str:
    return (
        getattr(args, "api_key", None)
        or os.environ.get("PANBRIDGE_API_KEY")
        or _load_api_key_file()
    )


def _request(
    base_url: str,
    api_key: str,
    method: str,
    path: str,
    payload: dict | None = None,
    timeout: float = 120.0,
) -> dict:
    url = base_url.rstrip("/") + path
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        code: str | None = None
        message = raw or f"HTTP {exc.code}"
        try:
            parsed = json.loads(raw)
            code = parsed.get("code")
            message = parsed.get("message") or parsed.get("detail") or raw
        except (ValueError, AttributeError):
            pass
        raise PanBridgeError(message, code) from exc
    except urllib.error.URLError as exc:
        raise PanBridgeError(
            f"cannot reach PanBridge at {base_url}: {exc.reason}", "CONNECTION"
        ) from exc


def _emit(obj: dict | list) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _run(args: argparse.Namespace) -> int:
    base_url = _resolve_base_url(args)
    api_key = _resolve_api_key(args)
    cmd = args.command

    if cmd == "health":
        _emit(_request(base_url, api_key, "GET", "/health"))
        return 0

    if cmd == "providers":
        _emit(_request(base_url, api_key, "GET", "/v1/providers"))
        return 0

    if cmd == "inspect":
        _emit(_request(base_url, api_key, "POST", "/v1/shares/inspect",
                       {"text": args.text}))
        return 0

    if cmd in ("download", "save", "save_and_download"):
        inp: dict = {
            "text": args.text,
            "action": cmd,
            "on_conflict": args.on_conflict,
        }
        if args.selection:
            inp["selection"] = args.selection
        if args.destination:
            inp["destination"] = args.destination
        if args.account_id:
            inp["account_id"] = args.account_id
        _emit(_request(base_url, api_key, "POST", "/v1/tasks", {"input": inp}))
        return 0

    if cmd in ("status", "task"):
        _emit(_request(base_url, api_key, "GET", f"/v1/tasks/{args.task_id}"))
        return 0

    if cmd == "tasks":
        limit = getattr(args, "limit", 10)
        _emit(_request(base_url, api_key, "GET", f"/v1/tasks?limit={limit}&offset=0"))
        return 0

    if cmd == "files":
        _emit(_request(base_url, api_key, "GET", f"/v1/tasks/{args.task_id}/files"))
        return 0

    if cmd == "cancel":
        _emit(_request(base_url, api_key, "POST", f"/v1/tasks/{args.task_id}/cancel"))
        return 0

    if cmd == "retry":
        _emit(_request(base_url, api_key, "POST", f"/v1/tasks/{args.task_id}/retry"))
        return 0

    if cmd == "accounts":
        _emit(_request(base_url, api_key, "GET", "/v1/accounts"))
        return 0

    if cmd == "login":
        _emit(_request(base_url, api_key, "POST",
                       f"/v1/auth/qr/start?provider={args.provider}"))
        return 0

    if cmd == "qr-status":
        _emit(_request(base_url, api_key, "GET",
                       f"/v1/auth/qr/{args.session_id}"))
        return 0

    if cmd == "login-status":
        _emit(_request(base_url, api_key, "GET",
                       f"/v1/auth/status?provider={args.provider}"))
        return 0

    raise PanBridgeError(f"unknown command: {cmd}", "USAGE")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="panbridge",
        description="PanBridge HTTP API 命令行客户端（dsh skill）",
    )
    parser.add_argument(
        "--base-url",
        help=f"PanBridge 服务地址（默认 {DEFAULT_BASE_URL}）",
    )
    parser.add_argument(
        "--api-key",
        help="鉴权密钥（对应服务端 PANBRIDGE_API_KEY）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="健康检查")
    sub.add_parser("providers", help="列出支持的网盘平台")

    p = sub.add_parser("inspect", help="解析分享链接，预览文件列表")
    p.add_argument("text", help="分享链接（可含提取码文案）")

    for action in ("download", "save", "save_and_download"):
        p = sub.add_parser(action, help=f"创建 {action} 任务")
        p.add_argument("text", help="分享链接（可含提取码文案）")
        p.add_argument("--selection", nargs="*", help="仅处理指定 file_id")
        p.add_argument("--destination", help="转存目标目录名")
        p.add_argument("--account-id", help="指定账号 id（默认自动选）")
        p.add_argument(
            "--on-conflict",
            choices=ON_CONFLICT,
            default="skip",
            help="同名文件冲突策略（默认 skip）",
        )

    for cmd, help_ in (("status", "查询单个任务"), ("task", "查询单个任务（别名）")):
        p = sub.add_parser(cmd, help=help_)
        p.add_argument("task_id")

    p = sub.add_parser("tasks", help="列出最近任务")
    p.add_argument("--limit", type=int, default=10)

    p = sub.add_parser("files", help="列出任务产出文件")
    p.add_argument("task_id")

    for cmd in ("cancel", "retry"):
        p = sub.add_parser(cmd, help=f"{cmd} 任务")
        p.add_argument("task_id")

    sub.add_parser("accounts", help="列出已登录网盘账号")

    p = sub.add_parser("login", help="启动扫码登录")
    p.add_argument("provider", help="平台名（quark/aliyun/115/123/uc）")

    p = sub.add_parser("qr-status", help="查询扫码登录状态")
    p.add_argument("session_id")

    p = sub.add_parser("login-status", help="查询某平台登录状态")
    p.add_argument("provider")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except PanBridgeError as exc:
        print(
            json.dumps(
                {"ok": False, "code": exc.code, "message": exc.message},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print('{"ok": false, "code": "INTERRUPTED"}', file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
