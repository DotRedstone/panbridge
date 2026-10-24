"""插件授权边界（astrbot-plugin/authz.py）合成事件测试。

覆盖接入统一 QQ 机器人前的关键安全行为：
    - 允许/拒绝（默认拒绝、未配置拒绝）
    - 同一用户编号跨平台实例（不同身份，不互通）
    - 未授权群 / 群白名单
    - 直接链接被动识别的开关与放行条件
    - 扫码登录会话归属（回调只属于发起者、单会话限制）
    - 任务归属（其他用户不可查询/操作）
    - 插件共存（不匹配的消息一律放行）

不依赖 astrbot 框架；main.py 的接线由真实 AstrBot 验收覆盖
（见 docs/bot-integration.md「验收边界」）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "astrbot-plugin"
sys.path.insert(0, str(PLUGIN_DIR))

from authz import (  # noqa: E402
    DENY_ADMIN_REQUIRED,
    DENY_BOT_NOT_ALLOWED,
    DENY_CAPABILITY_DISABLED,
    DENY_GROUP_NOT_ALLOWED,
    DENY_NO_CONFIG,
    DENY_NOT_AUTHORIZED,
    Capability,
    ConfigAuthorizer,
    LoginSessionRegistry,
    TaskOwnership,
    build_identity,
    can_manage_task,
)
from utils import find_pan_link, parse_args  # noqa: E402

# 虚构身份样例（绝不对应真实账号）
OWNER_QQ = build_identity("qq_official", "bot-001", "OPENID_OWNER")
FRIEND_QQ = build_identity("qq_official", "bot-001", "OPENID_FRIEND")
FRIEND_IN_GROUP = build_identity("qq_official", "bot-001", "OPENID_FRIEND", "G_10001")
STRANGER_QQ = build_identity("qq_official", "bot-001", "OPENID_STRANGER")
# 同一用户编号出现在另一平台实例（如 QQ 官方 + OneBot v11）→ 不同身份
SAME_NUM_OTHER_PLATFORM = build_identity("aiocqhttp", "bot-001", "OPENID_FRIEND")
# 同一平台但另一机器人实例
OTHER_BOT = build_identity("qq_official", "bot-002", "OPENID_FRIEND")

CONFIG = {
    "admins": ["qq_official:OPENID_OWNER"],
    "users": ["qq_official:OPENID_FRIEND"],
    "groups": ["qq_official:G_10001"],
}


def authz(**overrides) -> ConfigAuthorizer:
    return ConfigAuthorizer({**CONFIG, **overrides})


# ----------------------------------------------------------------------
# 默认拒绝与白名单
# ----------------------------------------------------------------------


def test_no_config_denies_everyone():
    """未配置授权时（如刚部署），所有人一律拒绝。"""
    az = ConfigAuthorizer({})
    for ident in (OWNER_QQ, FRIEND_QQ, STRANGER_QQ):
        d = az.authorize(ident, Capability.INSPECT, ident.scope)
        assert not d.allowed
        assert d.reason == DENY_NO_CONFIG


def test_stranger_denied_by_default():
    """同学/陌生人不在白名单 → 默认拒绝，知道 /pb 命令也没用。"""
    az = authz()
    for cap in Capability:
        d = az.authorize(STRANGER_QQ, cap, STRANGER_QQ.scope)
        assert not d.allowed
        assert d.reason == DENY_NOT_AUTHORIZED


def test_user_allowed_in_c2c():
    az = authz()
    d = az.authorize(FRIEND_QQ, Capability.INSPECT, "c2c")
    assert d.allowed
    d = az.authorize(FRIEND_QQ, Capability.DOWNLOAD, "c2c")
    assert d.allowed


def test_same_openid_across_platform_instances_is_distinct():
    """同一用户编号跨平台实例：不同身份，授权不互通。"""
    az = authz()
    # FRIEND_QQ 在 qq_official 已授权；同一编号在 aiocqhttp 上未授权
    assert az.role_of(FRIEND_QQ) == "user"
    assert az.role_of(SAME_NUM_OTHER_PLATFORM) is None
    d = az.authorize(
        SAME_NUM_OTHER_PLATFORM, Capability.INSPECT, SAME_NUM_OTHER_PLATFORM.scope
    )
    assert not d.allowed
    assert d.reason == DENY_NOT_AUTHORIZED


def test_bot_instance_allowlist():
    """多机器人实例接入时，可限定只有指定机器人实例能触发网盘功能。"""
    az = authz(allowed_bots=["bot-001"])
    assert az.authorize(FRIEND_QQ, Capability.INSPECT, "c2c").allowed
    d = az.authorize(OTHER_BOT, Capability.INSPECT, OTHER_BOT.scope)
    assert not d.allowed
    assert d.reason == DENY_BOT_NOT_ALLOWED


# ----------------------------------------------------------------------
# 群聊场景
# ----------------------------------------------------------------------


def test_unauthorized_group_denied():
    """授权用户在未授权群里 → 拒绝（群白名单按 平台:群ID 区分）。"""
    az = authz()
    d = az.authorize(FRIEND_QQ, Capability.INSPECT, "group:G_99999")
    assert not d.allowed
    assert d.reason == DENY_GROUP_NOT_ALLOWED


def test_authorized_group_allowed():
    az = authz()
    d = az.authorize(FRIEND_IN_GROUP, Capability.INSPECT, FRIEND_IN_GROUP.scope)
    assert d.allowed


def test_group_scope_not_leaked_across_platforms():
    """群白名单按平台实例区分：别的平台同名群号不放行。"""
    az = authz()
    other_platform_group = build_identity(
        "aiocqhttp", "bot-001", "OPENID_FRIEND", "G_10001"
    )
    d = az.authorize(
        other_platform_group, Capability.INSPECT, other_platform_group.scope
    )
    assert not d.allowed


# ----------------------------------------------------------------------
# 权限分层：普通使用 vs 管理账号
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "cap",
    [
        Capability.ACCOUNT_LOGIN,
        Capability.ACCOUNT_LIST,
        Capability.ACCOUNT_LOGOUT,
    ],
)
def test_account_capabilities_admin_only(cap):
    az = authz()
    d = az.authorize(FRIEND_QQ, cap, "c2c")
    assert not d.allowed
    assert d.reason == DENY_ADMIN_REQUIRED
    assert az.authorize(OWNER_QQ, cap, "c2c").allowed


def test_save_disabled_by_default_even_for_admin():
    """转存到共享账号：权限定义明确前全局关闭（对管理员也关闭）。"""
    az = authz()
    d = az.authorize(OWNER_QQ, Capability.SAVE, "c2c")
    assert not d.allowed
    assert d.reason == DENY_CAPABILITY_DISABLED


def test_save_enabled_explicitly():
    az = authz(enable_save=True)
    assert az.authorize(OWNER_QQ, Capability.SAVE, "c2c").allowed
    assert az.authorize(FRIEND_QQ, Capability.SAVE, "c2c").allowed
    d = az.authorize(STRANGER_QQ, Capability.SAVE, "c2c")
    assert not d.allowed
    assert d.reason == DENY_NOT_AUTHORIZED


# ----------------------------------------------------------------------
# 任务归属
# ----------------------------------------------------------------------


def test_task_ownership_isolated():
    """任务只属于创建者：其他用户（哪怕也是白名单用户）不可查询/操作。"""
    ownership = TaskOwnership()
    ownership.record("task-aaa", FRIEND_QQ.key)
    assert ownership.is_owner("task-aaa", FRIEND_QQ.key)
    assert not ownership.is_owner("task-aaa", OWNER_QQ.key)
    assert not can_manage_task(ownership, OWNER_QQ, "user", "task-aaa")
    assert can_manage_task(ownership, OWNER_QQ, "admin", "task-aaa")


def test_task_ownership_tasks_of():
    ownership = TaskOwnership()
    ownership.record("t1", FRIEND_QQ.key)
    ownership.record("t2", FRIEND_QQ.key)
    ownership.record("t3", OWNER_QQ.key)
    assert ownership.tasks_of(FRIEND_QQ.key) == ["t1", "t2"]
    assert ownership.tasks_of(STRANGER_QQ.key) == []


def test_unregistered_task_denied_for_non_admin():
    """插件重启后登记丢失：非管理员一律不可操作存量任务（fail-closed）。"""
    ownership = TaskOwnership()
    assert not can_manage_task(ownership, FRIEND_QQ, "user", "task-old")
    assert can_manage_task(ownership, OWNER_QQ, "admin", "task-old")


# ----------------------------------------------------------------------
# 扫码登录回调归属
# ----------------------------------------------------------------------


def test_login_session_single_per_identity():
    reg = LoginSessionRegistry()
    reg.register("sess-1", OWNER_QQ.key)
    assert reg.initiator_of("sess-1") == OWNER_QQ.key
    assert reg.has_active(OWNER_QQ.key)


def test_login_session_callback_belongs_to_initiator():
    """回调归属：会话只登记发起者，别人查不到、结束后即清除。"""
    reg = LoginSessionRegistry()
    reg.register("sess-2", OWNER_QQ.key)
    assert reg.initiator_of("sess-2") == OWNER_QQ.key
    assert reg.initiator_of("sess-2") != FRIEND_QQ.key
    reg.finish("sess-2")
    assert reg.initiator_of("sess-2") is None
    assert not reg.has_active(OWNER_QQ.key)


# ----------------------------------------------------------------------
# 插件共存与被动识别
# ----------------------------------------------------------------------


def test_non_pan_link_ignored():
    """作业插件或其他插件的普通消息：识别不出网盘链接 → 放行。"""
    hw = "今晚 8 点交作业，作业提交入口 https://homework.example.com/t/1"
    assert find_pan_link(hw) is None
    assert find_pan_link("签到打卡 #daily") is None
    assert find_pan_link("看看这个视频 https://b23.tv/av12345") is None


def test_pan_link_detected():
    link = find_pan_link("https://pan.quark.cn/s/abc123 提取码 1234")
    assert link is not None
    assert "pan.quark.cn" in link


def test_parse_args_ignores_other_commands():
    """别的插件的命令：前缀不会被误剥（parse_args 只认 /pb）。"""
    tokens = parse_args("/hw submit https://homework.example.com/t/1")
    # 第一个 token 仍是 "/hw"，不会被当成 /pb 子命令分发
    assert tokens[0] == "/hw"
    assert tokens[0].lstrip("/").lower() != "pb"


def test_identity_fields_are_distinct_dimensions():
    """身份四元组互相区分：平台实例、机器人实例、OpenID、群。"""
    a = build_identity("qq_official", "bot-1", "O1")
    assert a != build_identity("aiocqhttp", "bot-1", "O1")  # 平台实例
    assert a != build_identity("qq_official", "bot-2", "O1")  # 机器人实例
    assert a != build_identity("qq_official", "bot-1", "O2")  # OpenID
    assert a != build_identity("qq_official", "bot-1", "O1", "G_1")  # 群
