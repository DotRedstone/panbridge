"""插件授权边界（纯逻辑，不依赖 astrbot，可独立单测）。

角色分工：
    - 统一 QQ 机器人业务主线（如作业插件所在主线）负责确定用户群体与权限；
    - 本插件只**落实传入的授权结果**，不另建一套全局用户身份库；
    - PanBridge 后端只负责解析/转存/下载，通道鉴权用 X-API-Key（服务间）。

原则：
    - **默认拒绝**：授权配置缺失或身份不在白名单时一律拒绝；
    - 身份只采信 AstrBot 事件对象上的平台字段（平台实例、机器人实例、
      OpenID、群 ID），绝不采信昵称、消息正文中的自称、QQ 号文本等
      不可信来源，也不把后端 X-API-Key 当作用户身份；
    - 群聊使用要求群在白名单内（按「平台实例:群 ID」区分，避免把 A 群
      的授权带到 B 群）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Capability(StrEnum):
    """PanBridge 插件能力清单（对应 /pb 子命令）。"""

    INSPECT = "panbridge.inspect"  # /pb <链接> 解析预览、/pb providers、/pb help
    DOWNLOAD = "panbridge.download"  # /pb download
    SAVE = "panbridge.save"  # /pb save（默认关闭）
    TASK_VIEW = "panbridge.task.view"  # /pb status
    TASK_CANCEL = "panbridge.task.cancel"  # /pb cancel
    TASK_RETRY = "panbridge.task.retry"  # /pb retry
    ACCOUNT_LOGIN = "panbridge.account.login"  # /pb login（仅管理员）
    ACCOUNT_LIST = "panbridge.account.list"  # /pb accounts（仅管理员）
    ACCOUNT_LOGOUT = "panbridge.account.logout"  # 登出（仅管理员）


#: 仅所有者/管理员可用的能力（登录、账号列表等凭据相关操作）
ADMIN_ONLY: frozenset[Capability] = frozenset(
    {
        Capability.ACCOUNT_LOGIN,
        Capability.ACCOUNT_LIST,
        Capability.ACCOUNT_LOGOUT,
    }
)


@dataclass(frozen=True)
class TrustedIdentity:
    """可信平台身份。

    四个字段全部来自事件对象上的平台侧数据，用于互相区分：
      - platform:  平台实例（如 ``qq_official``），同一用户编号在不同
        平台实例上是**不同身份**；
      - bot_id:    机器人实例 ID（同一平台可挂多个机器人）；
      - openid:    平台侧用户 OpenID（不是 QQ 号原文，官方平台为映射 ID）；
      - scope:     会话范围：``group:<群ID>`` 或 ``c2c``（私聊）。
    """

    platform: str
    bot_id: str
    openid: str
    scope: str = "c2c"

    @property
    def key(self) -> str:
        """身份匹配键：平台实例 + OpenID。

        同一用户编号跨平台实例不会碰撞（键里含 platform）。
        """
        return f"{self.platform}:{self.openid}"


@dataclass(frozen=True)
class Decision:
    """授权判定结果。``allowed=False`` 时 ``reason`` 说明拒绝原因。"""

    allowed: bool
    reason: str = "ok"


#: 常用拒绝原因（便于测试与日志区分）
OK = "ok"
DENY_NO_CONFIG = "no_config"  # 授权配置缺失，全部拒绝
DENY_BOT_NOT_ALLOWED = "bot_not_allowed"
DENY_NOT_AUTHORIZED = "identity_not_authorized"  # 默认拒绝
DENY_GROUP_NOT_ALLOWED = "group_not_allowed"
DENY_ADMIN_REQUIRED = "admin_required"
DENY_CAPABILITY_DISABLED = "capability_disabled"

_ALLOW = Decision(True, OK)


def build_identity(
    platform: str, bot_id: str, openid: str, group_id: str | None = None
) -> TrustedIdentity:
    """由事件对象上的平台字段构造可信身份（纯函数，便于合成事件测试）。"""
    scope = f"group:{group_id}" if group_id else "c2c"
    return TrustedIdentity(
        platform=platform or "",
        bot_id=bot_id or "",
        openid=openid or "",
        scope=scope,
    )


@dataclass
class ConfigAuthorizer:
    """基于静态白名单的授权器（默认拒绝）。

    配置项（插件配置 dict，全部可选）：
      - ``admins``: 所有者/管理员身份键列表（``平台实例:OpenID``）
      - ``users``:  普通朋友身份键列表
      - ``groups``: 允许使用网盘功能的群列表（``平台实例:群ID``）；
        空 = 仅私聊可用
      - ``allowed_bots``: 允许接入的机器人实例 ID；空 = 不限制
      - ``enable_save``: 是否开放转存（默认 False；权限定义明确前保持关闭）

    admins 与 users 均为空时视为「未配置授权」，对所有身份拒绝。
    """

    admins: frozenset[str] = field(default_factory=frozenset)
    users: frozenset[str] = field(default_factory=frozenset)
    groups: frozenset[str] = field(default_factory=frozenset)
    allowed_bots: frozenset[str] = field(default_factory=frozenset)
    enable_save: bool = False

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.admins = frozenset(_as_list(cfg.get("admins")))
        self.users = frozenset(_as_list(cfg.get("users")))
        self.groups = frozenset(_as_list(cfg.get("groups")))
        self.allowed_bots = frozenset(_as_list(cfg.get("allowed_bots")))
        self.enable_save = bool(cfg.get("enable_save", False))

    @property
    def configured(self) -> bool:
        """是否配置了任何授权主体。未配置时全部拒绝（fail-closed）。"""
        return bool(self.admins or self.users)

    def role_of(self, identity: TrustedIdentity) -> str | None:
        """返回 ``admin`` / ``user`` / None（未授权）。"""
        if identity.key in self.admins:
            return "admin"
        if identity.key in self.users:
            return "user"
        return None

    def authorize(
        self,
        identity: TrustedIdentity,
        capability: Capability,
        scene: str = "c2c",
    ) -> Decision:
        """授权边界：authorize(可信平台身份, capability, 场景)。

        判定顺序（任一步不通过即拒绝）：
          1. 授权配置是否缺失（缺省拒绝）；
          2. 机器人实例是否在允许列表；
          3. 身份是否有角色（不在 admins/users 白名单 → 默认拒绝）；
          4. 场景：群聊要求群在白名单（``平台实例:群ID``）；
          5. 能力：凭据相关仅管理员；``save`` 受全局开关控制。
        """
        if not self.configured:
            return Decision(False, DENY_NO_CONFIG)
        if self.allowed_bots and identity.bot_id not in self.allowed_bots:
            return Decision(False, DENY_BOT_NOT_ALLOWED)
        role = self.role_of(identity)
        if role is None:
            return Decision(False, DENY_NOT_AUTHORIZED)
        if scene.startswith("group:"):
            group_key = f"{identity.platform}:{scene[len('group:'):]}"
            if group_key not in self.groups:
                return Decision(False, DENY_GROUP_NOT_ALLOWED)
        if capability in ADMIN_ONLY and role != "admin":
            return Decision(False, DENY_ADMIN_REQUIRED)
        if capability is Capability.SAVE and not self.enable_save:
            return Decision(False, DENY_CAPABILITY_DISABLED)
        return _ALLOW


class TaskOwnership:
    """插件本地的任务归属登记（内存态）。

    后端 API 是单一 X-API-Key 的服务间通道，没有用户概念；为避免
    A 用户查询/取消/重试 B 用户的任务，插件在创建任务时登记发起者，
    查询与操作前校验归属。插件重启后登记丢失，此时仅管理员可操作
    存量任务（fail-closed）。
    """

    def __init__(self) -> None:
        self._owners: dict[str, str] = {}

    def record(self, task_id: str, identity_key: str) -> None:
        self._owners[task_id] = identity_key

    def owner_of(self, task_id: str) -> str | None:
        return self._owners.get(task_id)

    def is_owner(self, task_id: str, identity_key: str) -> bool:
        return self._owners.get(task_id) == identity_key

    def tasks_of(self, identity_key: str) -> list[str]:
        """按创建顺序返回该身份登记的任务 ID（最新的在后）。"""
        return [t for t, k in self._owners.items() if k == identity_key]

    def forget(self, task_id: str) -> None:
        self._owners.pop(task_id, None)


def can_manage_task(
    ownership: TaskOwnership,
    identity: TrustedIdentity,
    role: str | None,
    task_id: str,
) -> bool:
    """任务归属校验：本人登记的任务，或管理员。"""
    if role == "admin":
        return True
    return ownership.is_owner(task_id, identity.key)


class LoginSessionRegistry:
    """扫码登录会话登记（内存态）。

    记录每个扫码会话的发起者身份键，保证：
      - 每个身份同时只有一个进行中的扫码会话；
      - 异步回执只发给发起者所在会话（main.py 据此取回执目标）。
    插件重启后登记丢失，剩余会话由轮询超时自然结束（fail-safe）。
    """

    def __init__(self) -> None:
        self._sessions: dict[str, str] = {}

    def has_active(self, identity_key: str) -> bool:
        return identity_key in self._sessions.values()

    def register(self, session_id: str, identity_key: str) -> None:
        self._sessions[session_id] = identity_key

    def initiator_of(self, session_id: str) -> str | None:
        return self._sessions.get(session_id)

    def finish(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


def _as_list(value) -> list[str]:
    """把配置值规整为去空白字符串列表（None/空 → 空列表）。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []
