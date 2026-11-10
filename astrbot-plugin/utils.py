"""AstrBot 插件纯函数工具，不依赖 astrbot 框架，便于单元测试。

包含参数解析、消息格式化、文件大小格式化、网盘链接识别等无副作用函数。
"""

from __future__ import annotations

import re
import shlex
from typing import Any

PREFIX = "pb"

# PanBridge 后端稳定的错误码（与 src/panbridge/domain/errors.py 对齐）。
# 插件侧仅据此分类追问，不硬编码具体网盘细节。
CODE_INVALID_PASSWORD = "INVALID_PASSWORD"
CODE_INVALID_SHARE = "INVALID_SHARE"
CODE_ACTION_REQUIRED = "ACTION_REQUIRED"

#: 预览文件列表的最大展示条数（保持简洁）。
INSPECT_FILE_LIMIT = 10

# 网盘分享链接（被动识别用；仅在 auto_detect_links 开启且授权通过时使用）
_LINK_RE = re.compile(
    r"https?://[^\s]+(?:"
    r"quark\.cn|aliyundrive\.com|alipan\.com|123pan\.com|123pan\.cn|"
    r"baidu\.com|115\.com|uc\.cn|xunlei\.com|weiyun\.com|lanzou|"
    r"woozooo\.com|189\.cn|cloud\.189\.cn|lanzouu\.com"
    r")[^\s]*"
)


def find_pan_link(text: str) -> str | None:
    """从消息文本中识别网盘分享链接；无匹配返回 None。

    注意：被动识别默认关闭（``auto_detect_links``），且开启后仍需
    授权与会话范围检查通过才处理；其余消息一律放行给其他插件。
    """
    if not text:
        return None
    m = _LINK_RE.search(text)
    return m.group(0) if m else None


def parse_args(text: str) -> list[str]:
    """去掉指令前缀后按空白切分参数（兼容带引号的提取码）。"""
    stripped = text.strip()
    tokens = stripped.split(maxsplit=1)
    if tokens and tokens[0].lstrip("/").lower() == PREFIX:
        stripped = tokens[1] if len(tokens) > 1 else ""
    if not stripped:
        return []
    try:
        return shlex.split(stripped)
    except ValueError:
        return stripped.split()


def format_inspect(info: dict[str, Any]) -> str:
    """把解析结果格式化为简洁预览（只读，不含下载/转存动作）。

    输出尽可能精炼：平台 + 标题 + 文件列表（上限 INSPECT_FILE_LIMIT 条），
    末尾附「下一步」提示，引导用户按需走 /pb 高级入口。
    """
    provider = info.get("provider", "?")
    title = info.get("title", "")
    files = info.get("files", [])
    lines = [f"平台：{provider}", f"标题：{title or '(无)'}"]
    shown = files[:INSPECT_FILE_LIMIT]
    for f in shown:
        name = f.get("name", "?")
        is_dir = f.get("is_dir")
        size = f.get("size")
        suffix = "/" if is_dir else fmt_size(size)
        lines.append(f"  - {name}{suffix}")
    total = len(files)
    if total > INSPECT_FILE_LIMIT:
        lines.append(f"  …（共 {total} 项，仅显示前 {INSPECT_FILE_LIMIT}）")
    elif total == 0:
        lines.append("  （空分享，无文件）")
    lines.append("")
    lines.append("如需下载请发送 /pb download <链接>。")
    return "\n".join(lines)


def classify_inspect_error(code: str | None) -> str:
    """把 PanBridge 解析错误码归类为面向用户的追问提示。

    返回稳定分类键，便于 main.py 据以生成「说明下一步」的追问文案：

      - ``need_password``  —— 提取码缺失或错误（INVALID_PASSWORD）；
      - ``need_account``   —— 该平台解析需要登录态（ACTION_REQUIRED）；
      - ``invalid_link``   —— 链接不完整或无法识别（INVALID_SHARE / 未知）；
      - ``unsupported``    —— 平台不支持（UNSUPPORTED_PROVIDER）；
      - ``error``          —— 其它错误（连接失败、限流等），不追问。

    分类键只用于选择文案，不泄露上游细节。
    """
    if code == CODE_INVALID_PASSWORD:
        return "need_password"
    if code == CODE_ACTION_REQUIRED:
        return "need_account"
    if code == CODE_INVALID_SHARE:
        return "invalid_link"
    if code == "UNSUPPORTED_PROVIDER":
        return "unsupported"
    return "error"


def inspect_followup_text(kind: str, message: str = "") -> str:
    """根据 classify_inspect_error 的分类生成追问文案（含下一步说明）。"""
    if kind == "need_password":
        return (
            "这个分享需要提取码，请把提取码一起发来，例如：\n"
            "  <链接> 提取码：xxxx"
        )
    if kind == "need_account":
        return (
            "该平台的分享解析需要登录账号，暂无法直接预览。\n"
            "请让管理员先登录对应网盘，或改用其他平台的分享。"
        )
    if kind == "invalid_link":
        return (
            "没识别到完整的网盘分享链接，请直接发送完整链接，例如：\n"
            "  https://pan.quark.cn/s/xxxx"
        )
    if kind == "unsupported":
        return "暂不支持这个网盘平台。"
    # 其它错误：附简短说明，避免暴露上游细节
    if message:
        return f"解析失败：{message}"
    return "解析失败，请稍后重试。"


def format_task(task: dict[str, Any]) -> str:
    """格式化任务状态。"""
    lines = [
        f"任务 {task.get('id', '?')}",
        f"动作：{task.get('action')}  状态：{task.get('status')}",
    ]
    if task.get("provider"):
        lines.append(f"平台：{task.get('provider')}")
    if task.get("error_message"):
        lines.append(f"错误：{task.get('error_message')}")
    result = task.get("result") or {}
    files = result.get("files") if isinstance(result, dict) else None
    if files:
        lines.append("产出文件：")
        for f in files:
            name = f.get("name") if isinstance(f, dict) else f
            lines.append(f"  - {name}")
    elif isinstance(result, dict) and result.get("saved") is not None:
        lines.append(
            f"转存结果：{result.get('saved')} → {result.get('destination', '')}"
        )
    return "\n".join(lines)


def fmt_size(size) -> str:
    """把字节数格式化为可读字符串；None 返回空。"""
    if size is None:
        return ""
    size = int(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f" ({size}{unit})" if unit == "B" else f" ({size:.1f}{unit})"
        size /= 1024
    return ""
