"""公共层：配置读取 + 渲染小工具，供各业务模块（burn/eta/...）复用。"""

from uptime.common.config import load_config
from uptime.common.render import (
    COLOR_ACCENT,
    COLOR_ALERT,
    COLOR_DIM,
    COLOR_OK,
    COLOR_WARN,
    STYLE_ALERT,
    STYLE_DIM,
    STYLE_OK,
    STYLE_TITLE,
    STYLE_WARN,
    auto_exit_seconds,
    get_now,
    money,
    set_console_title,
)

__all__ = [
    "load_config",
    "set_console_title",
    "money",
    "get_now",
    "auto_exit_seconds",
    # 颜色
    "COLOR_ACCENT",
    "COLOR_OK",
    "COLOR_WARN",
    "COLOR_ALERT",
    "COLOR_DIM",
    # 样式
    "STYLE_TITLE",
    "STYLE_OK",
    "STYLE_WARN",
    "STYLE_ALERT",
    "STYLE_DIM",
]
