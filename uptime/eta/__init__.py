"""eta 模块：倒计时面板。

工作日距下班倒计时（精确到秒）+ 下一法定假日倒计时 + 调休补班识别。
假日数据来自 data/holidays.json（每年手动更新；数据年份与当前年份不符时界面醒目提醒）。

运行：py -3.10 -m uptime.eta
测试钩子（与 burn 一致，见 uptime.common.render）：
  - UPTIME_FAKE_NOW="YYYY-MM-DDTHH:MM:SS"  注入当前时间，便于复现/截图
  - UPTIME_AUTO_EXIT=<秒数>                 运行指定秒数后自动退出
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from uptime.common.config import PROJECT_ROOT, load_config
from uptime.common.render import (
    COLOR_ACCENT,
    COLOR_ALERT,
    COLOR_DIM,
    COLOR_OK,
    COLOR_WARN,
    auto_exit_seconds,
    get_now,
    set_console_title,
)

# 模块代号（对外伪装统一用代号）
MODULE_CODE = "eta"
# 界面标题行（面板标题）
PANEL_TITLE = f"uptime — {MODULE_CODE}"
# 终端窗口标题（ASCII 连字符）
CONSOLE_TITLE = f"uptime - {MODULE_CODE}"

HOLIDAYS_PATH = PROJECT_ROOT / "data" / "holidays.json"

# 界面刷新间隔（秒）
REFRESH_INTERVAL = 1.0

# 今日状态种类
STATUS_ON = "on"            # 工作日·上班中
STATUS_OFF = "off"          # 工作日·已下班
STATUS_WEEKEND = "weekend"  # 休息日·周末
STATUS_HOLIDAY = "holiday"  # 休息日·法定假日


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------

def load_holidays(path: str | Path | None = None) -> dict[str, Any]:
    """读取并校验假日数据 data/holidays.json，返回 dict。

    结构：{year, spans: [{name, start, end}], off_days: {"YYYY-MM-DD": 假日名},
    extra_workdays: ["YYYY-MM-DD"]}。任何问题抛 ValueError（中文消息）。
    """
    p = Path(path) if path is not None else HOLIDAYS_PATH
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"假日数据文件不存在：{p}") from None
    except json.JSONDecodeError as e:
        raise ValueError(f"假日数据文件不是合法 JSON：{p}（{e}）") from None

    if not isinstance(data, dict):
        raise ValueError(f"假日数据根节点必须是 JSON 对象（{{...}}）：{p}")
    for key in ("year", "spans", "off_days", "extra_workdays"):
        if key not in data:
            raise ValueError(f"假日数据缺少字段 {key}：{p}")
    return data


# ---------------------------------------------------------------------------
# 核心逻辑
# ---------------------------------------------------------------------------

def is_extra_workday(d: date, holidays: dict[str, Any]) -> bool:
    """d 是否为调休补班日（在 extra_workdays 中）。"""
    return d.isoformat() in holidays["extra_workdays"]


def is_workday(d: date, cfg: dict[str, Any], holidays: dict[str, Any]) -> bool:
    """是否工作日：(星期几属于 config.workdays 且不在 off_days) 或 在 extra_workdays。"""
    key = d.isoformat()
    in_off = key in holidays["off_days"]
    return (d.weekday() in cfg["workdays"] and not in_off) or is_extra_workday(d, holidays)


def work_end_at(d: date, cfg: dict[str, Any]) -> datetime:
    """d 当天的下班时间 datetime（config.work_end，HH:MM）。"""
    return datetime.combine(d, dtime.fromisoformat(cfg["work_end"]))


def day_status(now: datetime, cfg: dict[str, Any], holidays: dict[str, Any]) -> dict[str, Any]:
    """今日状态。

    返回 {kind, ...}：
      on/weekend/holiday 无附加字段（weekday 状态下另带 remaining / name）
      - on:      remaining=距下班的 timedelta
      - off:     已下班
      - weekend: 今天不上班·周末
      - holiday: name=假日名（off_days 中）
    """
    d = now.date()
    if is_workday(d, cfg, holidays):
        end = work_end_at(d, cfg)
        if now < end:
            return {"kind": STATUS_ON, "remaining": end - now}
        return {"kind": STATUS_OFF}
    name = holidays["off_days"].get(d.isoformat())
    if name is not None:
        return {"kind": STATUS_HOLIDAY, "name": name}
    return {"kind": STATUS_WEEKEND}


def next_holiday(now: datetime, holidays: dict[str, Any]) -> dict[str, Any] | None:
    """下一假日。

    优先取 spans 中 start > 今天的最早者：{kind: "next", name, date, days}；
    若不存在（今天处于数据年度最后一个假日之中等），退回今天所在的假日：
    {kind: "current", name, end}；全年安排已过且今天不在假日中则返回 None。
    """
    today = now.date()
    spans = sorted(
        (date.fromisoformat(s["start"]), date.fromisoformat(s["end"]), s["name"])
        for s in holidays["spans"]
    )
    future = [s for s in spans if s[0] > today]
    if future:
        start, _end, name = future[0]
        return {"kind": "next", "name": name, "date": start, "days": (start - today).days}
    current = [s for s in spans if s[0] <= today <= s[1]]
    if current:
        _start, end, name = current[-1]
        return {"kind": "current", "name": name, "end": end}
    return None


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _fmt_hms(delta) -> str:
    """timedelta -> HH:MM:SS。"""
    total = max(0, int(delta.total_seconds()))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_md(d: date) -> str:
    """date -> M月D日（不补零）。"""
    return f"{d.month}月{d.day}日"


def _main_line(status: dict[str, Any]) -> Text:
    """主区第一行：距下班 / 已下班 / 今天不上班·周末 / 法定假日·<名>。"""
    kind = status["kind"]
    if kind == STATUS_ON:
        line = Text("距下班 ", style="bold")
        line.append(_fmt_hms(status["remaining"]), style=f"bold {COLOR_ACCENT}")
        return line
    if kind == STATUS_OFF:
        return Text("已下班", style=f"bold {COLOR_OK}")
    if kind == STATUS_HOLIDAY:
        line = Text("法定假日·", style=f"bold {COLOR_OK}")
        line.append(status["name"], style=f"bold {COLOR_ACCENT}")
        return line
    return Text("今天不上班·周末", style=f"bold {COLOR_OK}")


def _next_holiday_line(nh: dict[str, Any] | None) -> Text:
    """下一假日行。"""
    if nh is None:
        return Text("下一假日：本数据年度内暂无后续假日安排", style=COLOR_DIM)
    if nh["kind"] == "current":
        line = Text("下一假日：", style="bold")
        line.append("今天在假日中 · ")
        line.append(nh["name"], style=f"bold {COLOR_ACCENT}")
        line.append(f"（{_fmt_md(nh['end'])}结束）")
        return line
    line = Text("下一假日：", style="bold")
    line.append(nh["name"], style=f"bold {COLOR_ACCENT}")
    line.append(f" {_fmt_md(nh['date'])} 还有")
    line.append(str(nh["days"]), style=f"bold {COLOR_WARN}")
    line.append("天")
    return line


def render_panel(now: datetime, cfg: dict[str, Any], holidays: dict[str, Any]) -> Panel:
    """整帧面板（rich Live 每秒整帧刷新，无闪烁）。"""
    items: list[Text] = []

    # 数据年份不符 -> 醒目提醒（置顶）
    if holidays.get("year") != now.year:
        items.append(
            Text(
                f"注意：假日数据已过期（数据年份 {holidays.get('year')}，当前 {now.year}），"
                f"请更新 {HOLIDAYS_PATH.name}",
                style=f"bold {COLOR_ALERT}",
            )
        )
        items.append(Text())

    # 主区
    items.append(_main_line(day_status(now, cfg, holidays)))

    # 下一假日
    items.append(_next_holiday_line(next_holiday(now, holidays)))

    # 调休补班徽标（如适用）
    if is_extra_workday(now.date(), holidays):
        items.append(Text("[ 今日调休补班 ]", style=f"bold {COLOR_WARN}"))

    # 当前时刻
    items.append(Text())
    clock = Text("当前时刻 ", style=COLOR_DIM)
    clock.append(now.strftime("%H:%M:%S"), style="bold")
    items.append(clock)

    return Panel(
        Group(*items),
        title=Text(PANEL_TITLE, style=f"bold {COLOR_ACCENT}"),
        title_align="left",
        border_style=COLOR_DIM,
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> None:
    """主循环：rich Live 整帧刷新，UPTIME_AUTO_EXIT 到时自动退出，Ctrl+C 干净退出。"""
    cfg = load_config()
    holidays = load_holidays()
    set_console_title(CONSOLE_TITLE)

    exit_after = auto_exit_seconds()
    deadline = (time.monotonic() + exit_after) if exit_after is not None else None
    console = Console()

    def frame() -> Panel:
        return render_panel(get_now(), cfg, holidays)

    try:
        if not console.is_terminal:
            # 非终端（管道/重定向）：Live 无法逐帧重绘，输出一帧静态面板即可
            console.print(frame())
            return
        with Live(frame(), console=console, refresh_per_second=4, screen=False) as live:
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                live.update(frame())
                time.sleep(REFRESH_INTERVAL)
    except KeyboardInterrupt:
        return
