"""PanBridge AstrBot 插件。

让 AstrBot 机器人通过 PanBridge HTTP 服务完成多网盘分享解析、转存、下载与扫码登录。

指令：
    /pb <分享链接>            解析并预览分享文件列表
    /pb download <链接>       下载分享文件（可附加提取码）
    /pb save <链接>           转存分享文件到已登录账号
    /pb status [task_id]      查询任务状态（缺省列出最近任务）
    /pb login <provider>      扫码登录网盘（发送二维码图片）
    /pb accounts              列出已登录的网盘账号
    /pb providers             列出支持的网盘平台
    /pb help                  帮助

也可直接发送网盘链接，插件会自动识别并解析。
"""

from __future__ import annotations

import asyncio

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import MessageChain, Plain
from astrbot.api.star import Context, Star, register

from .panbridge_client import PanBridgeClient, PanBridgeError
from .utils import format_inspect, format_task, parse_args

# 指令前缀
PREFIX = "pb"

# 默认下载动作：download（分享直链）优先，失败时用户可改 save 转存
SUPPORTED_PROVIDERS = (
    "quark",
    "aliyun",
    "115",
    "123",
    "uc",
    "baidu",
    "cloud189",
    "caiyun",
    "xunlei",
    "weiyun",
    "guangya",
    "wukong",
    "pikpak",
    "lanzou",
)


@register(
    "astrbot_plugin_panbridge",
    "PanBridge",
    "通过 PanBridge 服务解析/转存/下载网盘分享链接",
    "1.0.0",
)
class PanBridgePlugin(Star):
    """PanBridge 网盘助手。

    配置项（在 AstrBot 插件配置里设置）：
      - base_url: PanBridge 服务地址，默认 http://127.0.0.1:8000
      - api_key:  服务端 PANBRIDGE_API_KEY（未配置鉴权时留空）
      - default_provider: 登录时缺省的平台，默认 quark
    """

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        config = config or {}
        self.base_url = config.get("base_url", "http://127.0.0.1:8000")
        self.api_key = config.get("api_key", "")
        self.default_provider = config.get("default_provider", "quark")
        self.client = PanBridgeClient(self.base_url, self.api_key)
        # session_id -> 登录轮询任务（登录完成后取消）
        self._login_tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # 主指令
    # ------------------------------------------------------------------
    @filter.command(PREFIX)
    async def pb(self, event: AstrMessageEvent):
        """PanBridge 网盘助手主指令。用法见 /pb help。"""
        args = parse_args(event.message_str)
        if not args:
            yield event.plain_result(self._help_text())
            return
        sub = args[0].lower()
        rest = args[1:]

        if sub in ("help", "帮助", "-h", "--help"):
            yield event.plain_result(self._help_text())
        elif sub in ("download", "down", "下载"):
            yield await self._handle_download(event, _join(rest))
        elif sub in ("save", "转存"):
            yield await self._handle_save(event, _join(rest))
        elif sub in ("status", "状态"):
            yield await self._handle_status(event, rest)
        elif sub in ("login", "登录"):
            yield await self._handle_login(event, rest)
        elif sub in ("accounts", "账号"):
            yield await self._handle_accounts(event)
        elif sub in ("providers", "平台"):
            yield await self._handle_providers(event)
        else:
            # 把整条消息当分享链接解析
            yield await self._handle_inspect(event, _join(args))

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    async def _handle_inspect(self, event: AstrMessageEvent, text: str):
        if not text:
            yield event.plain_result("请提供网盘分享链接。")
            return
        try:
            info = await self.client.inspect(text)
        except PanBridgeError as exc:
            yield event.plain_result(f"解析失败：{exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"连接 PanBridge 服务失败：{exc}")
            return
        yield event.plain_result(format_inspect(info))

    # ------------------------------------------------------------------
    # 下载 / 转存
    # ------------------------------------------------------------------
    async def _handle_download(self, event: AstrMessageEvent, text: str):
        if not text:
            yield event.plain_result("用法：/pb download <分享链接>")
            return
        async for r in self._submit_task(event, text, "download"):
            yield r

    async def _handle_save(self, event: AstrMessageEvent, text: str):
        if not text:
            yield event.plain_result("用法：/pb save <分享链接>")
            return
        async for r in self._submit_task(event, text, "save"):
            yield r

    async def _submit_task(self, event: AstrMessageEvent, text: str, action: str):
        try:
            task = await self.client.create_task(text, action)
        except PanBridgeError as exc:
            yield event.plain_result(f"提交失败：{exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"连接 PanBridge 服务失败：{exc}")
            return
        task_id = task.get("id", "?")
        yield event.plain_result(
            f"已创建任务 {task_id}（{action}），正在排队处理。\n"
            f"发送 /pb status {task_id} 查询进度。"
        )

    # ------------------------------------------------------------------
    # 任务状态
    # ------------------------------------------------------------------
    async def _handle_status(self, event: AstrMessageEvent, rest: list[str]):
        if rest:
            task_id = rest[0]
            try:
                task = await self.client.get_task(task_id)
            except PanBridgeError as exc:
                yield event.plain_result(f"查询失败：{exc.message}")
                return
            yield event.plain_result(format_task(task))
            return
        # 缺省列出最近任务
        try:
            data = await self.client.list_tasks(limit=5)
        except PanBridgeError as exc:
            yield event.plain_result(f"查询失败：{exc.message}")
            return
        items = data.get("items", [])
        if not items:
            yield event.plain_result("暂无任务。")
            return
        lines = ["最近任务："]
        for t in items:
            lines.append(
                f"  {t.get('id')}  {t.get('action')}  {t.get('status')}  "
                f"{t.get('provider') or '-'}"
            )
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 登录
    # ------------------------------------------------------------------
    async def _handle_login(self, event: AstrMessageEvent, rest: list[str]):
        if not rest:
            yield event.plain_result(
                "用法：/pb login <provider>\n"
                "可扫码平台：quark / aliyun / 115 / 123 / uc\n"
                "其余平台（百度/天翼/迅雷/微云等）需在 PanBridge 主机上手工导入凭据。"
            )
            return
        provider = rest[0].lower()
        if provider not in SUPPORTED_PROVIDERS:
            yield event.plain_result(f"未知平台 {provider}。可用：/pb providers")
            return
        try:
            result = await self.client.start_qr_login(provider)
        except PanBridgeError as exc:
            yield event.plain_result(f"发起扫码失败：{exc.message}")
            return
        session_id = result.get("session_id")
        qr_url = result.get("qr_url")
        if not qr_url:
            yield event.plain_result("未返回二维码，请稍后重试。")
            return
        # 发送二维码图片 + 提示
        yield event.plain_result(
            f"请用 {provider} 对应 App 扫码登录（二维码见下，5 分钟内有效）。"
        )
        yield event.image_result(qr_url)
        # 后台轮询登录状态
        self._start_login_poll(session_id, provider, event)

    def _start_login_poll(
        self, session_id: str, provider: str, event: AstrMessageEvent
    ):
        task = asyncio.create_task(
            self._poll_login(session_id, provider, event)
        )
        self._login_tasks[session_id] = task

    async def _poll_login(
        self, session_id: str, provider: str, event: AstrMessageEvent
    ):
        # 后台任务无法 yield，改用 event.send 主动推送结果。
        for _ in range(150):  # 最多 5 分钟
            await asyncio.sleep(2)
            try:
                status = await self.client.check_qr_login(session_id)
            except Exception:  # noqa: BLE001
                continue
            st = status.get("status")
            if st == "success":
                account = status.get("account") or {}
                label = account.get("label") or account.get("id", "")
                await event.send(
                    MessageChain([Plain(f"✅ {provider} 登录成功（账号 {label}）。")])
                )
                break
            if st in ("expired", "cancelled", "failed"):
                await event.send(
                    MessageChain(
                        [
                            Plain(
                                f"登录未完成：{st}，请重新发送 /pb login {provider}。"
                            )
                        ]
                    )
                )
                break
        self._login_tasks.pop(session_id, None)

    # ------------------------------------------------------------------
    # 账号 / 平台列表
    # ------------------------------------------------------------------
    async def _handle_accounts(self, event: AstrMessageEvent):
        try:
            accounts = await self.client.list_accounts()
        except PanBridgeError as exc:
            yield event.plain_result(f"查询失败：{exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"连接失败：{exc}")
            return
        if not accounts:
            yield event.plain_result(
                "暂无已登录账号。用 /pb login <provider> 扫码登录。"
            )
            return
        lines = ["已登录账号："]
        for a in accounts:
            lines.append(
                f"  {a.get('provider')}  {a.get('label') or a.get('id')}"
            )
        yield event.plain_result("\n".join(lines))

    async def _handle_providers(self, event: AstrMessageEvent):
        try:
            data = await self.client.list_providers()
        except PanBridgeError as exc:
            yield event.plain_result(f"查询失败：{exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"连接失败：{exc}")
            return
        providers = data.get("providers", [])
        if not providers:
            yield event.plain_result("服务端未返回平台列表。")
            return
        lines = ["支持平台："]
        for p in providers:
            name = p.get("name") if isinstance(p, dict) else p
            lines.append(f"  - {name}")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 帮助
    # ------------------------------------------------------------------
    def _help_text(self) -> str:
        return (
            "PanBridge 网盘助手\n"
            "────────────────\n"
            "/pb <链接>            解析分享\n"
            "/pb download <链接>   下载分享文件\n"
            "/pb save <链接>       转存到已登录账号\n"
            "/pb status [id]       查任务状态\n"
            "/pb login <provider>  扫码登录（quark/aliyun/115/123/uc）\n"
            "/pb accounts          已登录账号\n"
            "/pb providers         支持平台\n"
            "也可直接发送网盘链接自动解析。"
        )

    async def terminate(self):
        for task in self._login_tasks.values():
            task.cancel()


# ----------------------------------------------------------------------
# 直接发链接也能触发解析（正则兜底）
# ----------------------------------------------------------------------
_LINK_RE = (
    r"(https?://[^\s]+(?:quark\.cn|aliyundrive\.com|alipan\.com|123pan\.com|"
    r"123pan\.cn|baidu\.com|115\.com|uc\.cn|xunlei\.com|weiyun\.com|"
    r"pan\.baidu\.com|share\.123pan\.cn|drive\.uc\.cn|lanzou\.|woozooo\.com|"
    r"189\.cn|cloud\.189\.cn)[^\s]*)"
)


def _join(parts: list[str]) -> str:
    return " ".join(parts)
