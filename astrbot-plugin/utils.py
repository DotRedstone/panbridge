"""AstrBot 插件纯函数工具，不依赖 astrbot 框架，便于单元测试。

包含参数解析、消息格式化、文件大小格式化等无副作用函数。
"""

from __future__ import annotations

import shlex
from typing import Any

PREFIX = "pb"


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
    """格式化解析结果。"""
    provider = info.get("provider", "?")
    title = info.get("title", "")
    files = info.get("files", [])
    lines = [f"平台：{provider}", f"标题：{title or '(无)'}", f"文件数：{len(files)}"]
    for f in files[:20]:
        name = f.get("name", "?")
        is_dir = f.get("is_dir")
        size = f.get("size")
        suffix = "/" if is_dir else fmt_size(size)
        lines.append(f"  - {name}{suffix}")
    if len(files) > 20:
        lines.append(f"  …（共 {len(files)} 项，仅显示前 20）")
    return "\n".join(lines)


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
