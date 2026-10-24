"""PanBridge AstrBot 插件。

让 AstrBot 机器人通过 PanBridge HTTP 服务完成多网盘分享解析、转存、下载与扫码登录。

角色分工：
    - 统一机器人业务主线（如作业插件）负责确定用户群体与权限；
    - 本插件只落实传入的授权结果（见 authz.py），**默认拒绝**，
      不维护自己的全局用户身份库；
    - PanBridge 后端只负责解析/转存/下载（X-API-Key 为服务间通道鉴权）。

指令（未授权用户使用 /pb 只会得到统一的「未开放」提示）：
    /pb <分享链接>            解析并预览分享文件列表
    /pb download <链接>       下载分享文件（可附加提取码）
    /pb save <链接>           转存分享文件到已登录账号（默认关闭）
    /pb status [task_id]      查询任务状态（普通用户只能看自己创建的任务）
    /pb cancel <task_id>      取消自己创建的任务
    /pb retry <task_id>       重试自己创建的任务
    /pb login <provider>      扫码登录网盘（仅管理员）
    /pb accounts              列出已登录的网盘账号（仅管理员）
    /pb providers             列出支持的网盘平台
    /pb help                  帮助

直接发送网盘链接的被动识别**默认关闭**（auto_detect_links），开启后也仅在
授权与会话范围检查通过时处理；未授权/不匹配的消息一律静默放行，不吞掉
作业插件或其他插件的任何消息。
"""

from __future__ import annotations

import asyncio

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import MessageChain, Plain
from astrbot.api.star import Context, Star, register

from .authz import (
    Capability,
    ConfigAuthorizer,
    Decision,
    LoginSessionRegistry,
    TaskOwnership,
    TrustedIdentity,
    build_identity,
    can_manage_task,
)
from .panbridge_client import PanBridgeClient, PanBridgeError
from .utils import find_pan_link, format_inspect, format_task, parse_args

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
    "pikpak",
    "lanzou",
)


@register(
    "astrbot_plugin_panbridge",
    "PanBridge",
    "通过 PanBridge 服务解析/转存/下载网盘分享链接",
    "1.1.0",
)
class PanBridgePlugin(Star):
    """PanBridge 网盘助手（带授权边界）。

    配置项（在 AstrBot 插件配置里设置）：
      - base_url: PanBridge 服务地址，默认 http://127.0.0.1:8000
      - api_key:  服务端 PANBRIDGE_API_KEY（仅服务间通道鉴权，不是用户身份）
      - default_provider: 登录时缺省的平台，默认 quark

    授权配置（缺省全部拒绝，见 authz.py）：
      - admins: 所有者/管理员身份键列表（"平台实例:OpenID"）
      - users:  普通朋友身份键列表
      - groups: 允许使用的群列表（"平台实例:群ID"；空 = 仅私聊）
      - allowed_bots: 允许接入的机器人实例 ID（空 = 不限制）
      - enable_save: 是否开放转存（默认 False）
      - auto_detect_links: 直接发链接的被动识别（默认 False）
    """

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        config = config or {}
        self.base_url = config.get("base_url", "http://127.0.0.1:8000")
        self.api_key = config.get("api_key", "")
        self.default_provider = config.get("default_provider", "quark")
        self.auto_detect_links = bool(config.get("auto_detect_links", False))
        self.authz = ConfigAuthorizer(config)
        self.client = PanBridgeClient(self.base_url, self.api_key)
        # 任务归属登记（内存态；重启后仅管理员可操作存量任务）
        self.ownership = TaskOwnership()
        # 扫码会话登记：发起者归属 + 每身份单会话限制
        self._login_sessions = LoginSessionRegistry()
        # session_id -> 登录轮询任务（登录完成后取消）
        self._login_tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # 身份提取与授权
    # ------------------------------------------------------------------
    @staticmethod
    def _identity_of(event: AstrMessageEvent) -> TrustedIdentity:
        """从事件对象提取可信身份。

        只读平台侧字段（平台实例 / 机器人实例 / OpenID / 群 ID），
        不读昵称、不解析消息正文里的自称。
        """
        # 平台实例优先用 platform_id（适配器实例 ID），退回平台类型名
        platform = str(
            getattr(event, "platform_id", "") or getattr(event, "platform", "") or ""
        )
        bot_id = str(getattr(event, "bot_id", "") or "")
        if not bot_id:
            message_obj = getattr(event, "message_obj", None)
            bot_id = str(getattr(message_obj, "self_id", "") or "")
        get_sender = getattr(event, "get_sender_id", None)
        openid = str(get_sender() or "") if callable(get_sender) else ""
        get_group = getattr(event, "get_group_id", None)
        group_id = str(get_group() or "") if callable(get_group) else ""
        return build_identity(platform, bot_id, openid, group_id or None)

    @staticmethod
    def _scene_of(identity: TrustedIdentity) -> str:
        """会话场景：group:<群ID> 或 c2c。"""
        return identity.scope

    def _authorize(
        self, event: AstrMessageEvent, capability: Capability
    ) -> tuple[TrustedIdentity, Decision]:
        identity = self._identity_of(event)
        return identity, self.authz.authorize(
            identity, capability, self._scene_of(identity)
        )

    @staticmethod
    def _denied_text(decision: Decision) -> str:
        """未授权时的统一对外提示（不暴露原因细节，避免被探测）。"""
        return "PanBridge 未对你的账号开放。"

    # ------------------------------------------------------------------
    # 主指令
    # ------------------------------------------------------------------
    @filter.command(PREFIX)
    async def pb(self, event: AstrMessageEvent):
        """PanBridge 网盘助手主指令。用法见 /pb help。"""
        args = parse_args(event.message_str)
        if not args:
            identity, decision = self._authorize(event, Capability.INSPECT)
            if not decision.allowed:
                yield event.plain_result(self._denied_text(decision))
                return
            yield event.plain_result(self._help_text())
            return
        sub = args[0].lower()
        rest = args[1:]

        if sub in ("help", "帮助", "-h", "--help"):
            _, decision = self._authorize(event, Capability.INSPECT)
            if not decision.allowed:
                yield event.plain_result(self._denied_text(decision))
                return
            yield event.plain_result(self._help_text())
        elif sub in ("providers", "平台"):
            _, decision = self._authorize(event, Capability.INSPECT)
            if not decision.allowed:
                yield event.plain_result(self._denied_text(decision))
                return
            async for r in self._handle_providers(event):
                yield r
        elif sub in ("download", "down", "下载"):
            identity, decision = self._authorize(event, Capability.DOWNLOAD)
            if not decision.allowed:
                yield event.plain_result(self._denied_text(decision))
                return
            async for r in self._handle_download(event, _join(rest), identity):
                yield r
        elif sub in ("save", "转存"):
            identity, decision = self._authorize(event, Capability.SAVE)
            if not decision.allowed:
                yield event.plain_result(self._denied_text(decision))
                return
            async for r in self._handle_save(event, _join(rest), identity):
                yield r
        elif sub in ("status", "状态"):
            identity, decision = self._authorize(event, Capability.TASK_VIEW)
            if not decision.allowed:
                yield event.plain_result(self._denied_text(decision))
                return
            async for r in self._handle_status(event, rest, identity):
                yield r
        elif sub in ("cancel", "取消"):
            identity, decision = self._authorize(event, Capability.TASK_CANCEL)
            if not decision.allowed:
                yield event.plain_result(self._denied_text(decision))
                return
            async for r in self._handle_cancel(event, rest, identity):
                yield r
        elif sub in ("retry", "重试"):
            identity, decision = self._authorize(event, Capability.TASK_RETRY)
            if not decision.allowed:
                yield event.plain_result(self._denied_text(decision))
                return
            async for r in self._handle_retry(event, rest, identity):
                yield r
        elif sub in ("login", "登录"):
            _, decision = self._authorize(event, Capability.ACCOUNT_LOGIN)
            if not decision.allowed:
                yield event.plain_result(self._denied_text(decision))
                return
            async for r in self._handle_login(event, rest):
                yield r
        elif sub in ("accounts", "账号"):
            _, decision = self._authorize(event, Capability.ACCOUNT_LIST)
            if not decision.allowed:
                yield event.plain_result(self._denied_text(decision))
                return
            async for r in self._handle_accounts(event):
                yield r
        else:
            # 把整条消息当分享链接解析
            identity, decision = self._authorize(event, Capability.INSPECT)
            if not decision.allowed:
                yield event.plain_result(self._denied_text(decision))
                return
            async for r in self._handle_inspect(event, _join(args)):
                yield r

    # ------------------------------------------------------------------
    # 被动链接识别（默认关闭；不吞其他插件消息）
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """被动识别网盘链接。

        仅当 auto_detect_links 开启、消息含网盘链接、且授权+会话范围
        检查通过时才处理；其余情况直接返回（不产出消息、不 stop_event），
        作业插件和其他插件不受影响。
        """
        if not self.auto_detect_links:
            return
        text = (event.message_str or "").strip()
        if not text:
            return
        # 命令交给命令处理器，避免重复响应
        if text.lstrip("/").lower().startswith(PREFIX):
            return
        link = find_pan_link(text)
        if not link:
            return
        _, decision = self._authorize(event, Capability.INSPECT)
        if not decision.allowed:
            # 未授权：静默忽略，绝不拦截事件
            return
        async for r in self._handle_inspect(event, text):
            yield r

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
    async def _handle_download(
        self, event: AstrMessageEvent, text: str, identity: TrustedIdentity
    ):
        if not text:
            yield event.plain_result("用法：/pb download <分享链接>")
            return
        async for r in self._submit_task(event, text, "download", identity):
            yield r

    async def _handle_save(
        self, event: AstrMessageEvent, text: str, identity: TrustedIdentity
    ):
        if not text:
            yield event.plain_result("用法：/pb save <分享链接>")
            return
        async for r in self._submit_task(event, text, "save", identity):
            yield r

    async def _submit_task(
        self,
        event: AstrMessageEvent,
        text: str,
        action: str,
        identity: TrustedIdentity,
    ):
        try:
            task = await self.client.create_task(text, action)
        except PanBridgeError as exc:
            yield event.plain_result(f"提交失败：{exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"连接 PanBridge 服务失败：{exc}")
            return
        task_id = task.get("id", "?")
        self.ownership.record(task_id, identity.key)
        yield event.plain_result(
            f"已创建任务 {task_id}（{action}），正在排队处理。\n"
            f"发送 /pb status {task_id} 查询进度。"
        )

    # ------------------------------------------------------------------
    # 任务状态 / 取消 / 重试（带归属校验）
    # ------------------------------------------------------------------
    def _can_touch_task(
        self, identity: TrustedIdentity, role: str | None, task_id: str
    ) -> bool:
        return can_manage_task(self.ownership, identity, role, task_id)

    async def _handle_status(
        self, event: AstrMessageEvent, rest: list[str], identity: TrustedIdentity
    ):
        role = self.authz.role_of(identity)
        if rest:
            task_id = rest[0]
            if not self._can_touch_task(identity, role, task_id):
                # 不存在或不是你的任务：统一提示，不泄露任务是否存在
                yield event.plain_result("没有这个任务的查看权限。")
                return
            try:
                task = await self.client.get_task(task_id)
            except PanBridgeError as exc:
                yield event.plain_result(f"查询失败：{exc.message}")
                return
            yield event.plain_result(format_task(task))
            return
        # 缺省列表：普通用户只列自己登记的任务；管理员列全局最近任务
        if role == "admin":
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
            return
        own_ids = self.ownership.tasks_of(identity.key)
        if not own_ids:
            yield event.plain_result("暂无你的任务。")
            return
        lines = ["你的任务："]
        for task_id in own_ids[-5:]:
            try:
                task = await self.client.get_task(task_id)
            except PanBridgeError:
                lines.append(f"  {task_id}  （查询失败）")
                continue
            lines.append(
                f"  {task.get('id', task_id)}  {task.get('action')}  "
                f"{task.get('status')}"
            )
        yield event.plain_result("\n".join(lines))

    async def _handle_cancel(
        self, event: AstrMessageEvent, rest: list[str], identity: TrustedIdentity
    ):
        if not rest:
            yield event.plain_result("用法：/pb cancel <task_id>")
            return
        role = self.authz.role_of(identity)
        task_id = rest[0]
        if not self._can_touch_task(identity, role, task_id):
            yield event.plain_result("没有这个任务的操作权限。")
            return
        try:
            task = await self.client.cancel_task(task_id)
        except PanBridgeError as exc:
            yield event.plain_result(f"取消失败：{exc.message}")
            return
        yield event.plain_result(
            f"任务 {task_id} 状态：{task.get('status', '?')}"
        )

    async def _handle_retry(
        self, event: AstrMessageEvent, rest: list[str], identity: TrustedIdentity
    ):
        if not rest:
            yield event.plain_result("用法：/pb retry <task_id>")
            return
        role = self.authz.role_of(identity)
        task_id = rest[0]
        if not self._can_touch_task(identity, role, task_id):
            yield event.plain_result("没有这个任务的操作权限。")
            return
        try:
            task = await self.client.retry_task(task_id)
        except PanBridgeError as exc:
            yield event.plain_result(f"重试失败：{exc.message}")
            return
        yield event.plain_result(
            f"任务 {task_id} 状态：{task.get('status', '?')}"
        )

    # ------------------------------------------------------------------
    # 登录（仅管理员；回执只发给发起会话）
    # ------------------------------------------------------------------
    async def _handle_login(self, event: AstrMessageEvent, rest: list[str]):
        if not rest:
            yield event.plain_result(
                "用法：/pb login <provider>\n"
                "可扫码平台：quark / aliyun / 115 / 123 / uc\n"
                "其余平台（百度/天翼/迅雷/微云等）需在 PanBridge 主机上手工导入凭据。"
            )
            return
        identity = self._identity_of(event)
        provider = rest[0].lower()
        if provider not in SUPPORTED_PROVIDERS:
            yield event.plain_result(f"未知平台 {provider}。可用：/pb providers")
            return
        # 同一身份同时只允许一个扫码会话，防止会话堆积
        if self._login_sessions.has_active(identity.key):
            yield event.plain_result("你已有一个进行中的扫码登录，请先等它完成。")
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
        # 登记发起者：轮询回执只发到发起会话
        self._login_sessions.register(session_id, identity.key)
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
        # 注意：回执只发到发起登录的那个会话（event 即发起事件），
        # 且 QQ 官方平台主动消息受限（见 docs/bot-integration.md）。
        try:
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
                        MessageChain(
                            [Plain(f"✅ {provider} 登录成功（账号 {label}）。")]
                        )
                    )
                    break
                if st in ("expired", "cancelled", "failed"):
                    await event.send(
                        MessageChain(
                            [
                                Plain(
                                    "登录未完成："
                                    f"{st}，请重新发送 /pb login {provider}。"
                                )
                            ]
                        )
                    )
                    break
        finally:
            self._login_tasks.pop(session_id, None)
            self._login_sessions.finish(session_id)

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
        save_line = (
            "/pb save <链接>       转存到已登录账号\n" if self.authz.enable_save else ""
        )
        return (
            "PanBridge 网盘助手\n"
            "────────────────\n"
            "/pb <链接>            解析分享\n"
            "/pb download <链接>   下载分享文件\n"
            f"{save_line}"
            "/pb status [id]       查任务状态（自己的任务）\n"
            "/pb cancel <id>       取消自己的任务\n"
            "/pb retry <id>        重试自己的任务\n"
            "/pb login <provider>  扫码登录（仅管理员）\n"
            "/pb accounts          已登录账号（仅管理员）\n"
            "/pb providers         支持平台"
        )

    async def terminate(self):
        for task in self._login_tasks.values():
            task.cancel()


# ----------------------------------------------------------------------
# 被动识别所需的消息工具已移至 utils.find_pan_link（可独立单测）
# ----------------------------------------------------------------------


def _join(parts: list[str]) -> str:
    return " ".join(parts)
