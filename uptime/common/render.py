"""公共渲染小工具：终端标题、金额格式化、时间注入、自动退出、统一主题。"""

from __future__ import annotations

import os
import sys
from datetime import datetime

from rich.style import Style

# ---------------------------------------------------------------------------
# 主题常量：各模块统一观感（深色终端监控面板风格）
# ---------------------------------------------------------------------------
# 颜色名（rich 支持的标准色名，供 Text/Panel 等直接引用）
COLOR_ACCENT = "cyan"           # 主色：标题、高亮数字
COLOR_OK = "green"              # 正常态
COLOR_WARN = "yellow"           # 临近阈值
COLOR_ALERT = "red"             # 告警
COLOR_DIM = "bright_black"      # 次要信息、脚注

# 现成样式（Style 对象，供 Table/Text 等统一使用）
STYLE_TITLE = Style(color=COLOR_ACCENT, bold=True)
STYLE_OK = Style(color=COLOR_OK)
STYLE_WARN = Style(color=COLOR_WARN)
STYLE_ALERT = Style(color=COLOR_ALERT, bold=True)
STYLE_DIM = Style(dim=True)


def set_console_title(text: str) -> None:
    """设置终端窗口标题：写 ANSI OSC 序列（ESC ]0; text BEL）并立即 flush。"""
    sys.stdout.write(f"\x1b]0;{text}\x07")
    sys.stdout.flush()


def money(n: float) -> str:
    """金额格式化：¥ 前缀 + 千分位 + 两位小数，如 1234.5 -> '¥1,234.50'。"""
    return f"¥{n:,.2f}"


def get_now() -> datetime:
    """当前时间。

    环境变量 UPTIME_FAKE_NOW 可解析（datetime.fromisoformat 格式，
    如 2026-09-02T09:30:00）时返回该注入时间，否则返回 datetime.now()。
    供测试/截图复现使用。
    """
    raw = os.environ.get("UPTIME_FAKE_NOW")
    if raw:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    return datetime.now()


def auto_exit_seconds() -> int | None:
    """自动退出秒数：UPTIME_AUTO_EXIT 为正整数时返回该秒数，否则 None。

    供演示/截图场景下模块运行指定时长后自动退出。
    """
    raw = os.environ.get("UPTIME_AUTO_EXIT")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None
