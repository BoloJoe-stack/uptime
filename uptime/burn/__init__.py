"""burn · burn-rate 实时监控面板。

按月薪折算秒级费率，rich Live 每秒整帧刷新"今日已赚"。
时间逻辑全部走 uptime.common.render.get_now()（UPTIME_FAKE_NOW 可注入），
演示退出走 UPTIME_AUTO_EXIT。

计算口径：
- 日薪 = 月薪 / 月工作日
- 有效计费秒 = (班内总分钟 - 午休与班内重叠分钟) * 60
- 秒费率 = 日薪 / 有效计费秒
- 已赚 = work_start 到当前时刻的班内有效时长（剔除午休 12:00~13:00）× 秒费率
- 进度 = (now - work_start) / (work_end - work_start)，纯时钟比例，仅显示用
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta

from rich.console import Group, RenderableType
from rich.live import Live
from rich.table import Table
from rich.text import Text

from uptime.common import (
    auto_exit_seconds,
    get_now,
    load_config,
    money,
    set_console_title,
)
from uptime.eta import is_workday

# 窗口标题：对外伪装格式（ASCII 连字符）
WINDOW_TITLE = "uptime - burn"
# 面板标题行（界面内展示）
PANEL_TITLE = "uptime — burn"

# 午休固定从 12:00 起，时长读配置 lunch_break_minutes（默认 60 → 12:00~13:00）
_LUNCH_START = dtime(12, 0)

# 进度条宽度
_BAR_WIDTH = 22


@dataclass
class BurnStats:
    """一帧面板所需的全部计算结果。"""

    state: str  # "before" 未到上班 / "during" 班内 / "after" 已下班
    earned: float  # 今日已赚
    progress: float  # 0.0 ~ 1.0，纯时钟比例
    day_salary: float  # 日薪
    per_second: float  # 秒费率
    per_minute: float  # 每分费率
    per_hour: float  # 每时费率


def _parse_hhmm(text: str) -> dtime:
    hour, minute = text.split(":")
    return dtime(int(hour), int(minute))


def _overlap_minutes(a1: datetime, a2: datetime, b1: datetime, b2: datetime) -> float:
    """两时间段 [a1,a2] 与 [b1,b2] 的重叠分钟数。"""
    lo = max(a1, b1)
    hi = min(a2, b2)
    return max((hi - lo).total_seconds(), 0.0) / 60.0


def compute_stats(cfg: dict, now: datetime) -> BurnStats:
    """按计算口径算出当前时刻的面板数据。"""
    start = datetime.combine(now.date(), _parse_hhmm(cfg["work_start"]))
    end = datetime.combine(now.date(), _parse_hhmm(cfg["work_end"]))
    lunch_start = datetime.combine(now.date(), _LUNCH_START)
    lunch_end = lunch_start + timedelta(minutes=float(cfg.get("lunch_break_minutes", 60)))

    day_salary = cfg["monthly_salary"] / cfg["monthly_workdays"]

    total_minutes = (end - start).total_seconds() / 60.0
    # 午休与班内时段的重叠部分才需要剔除（如半天班）
    lunch_in_work = _overlap_minutes(start, end, lunch_start, lunch_end)
    billable_seconds = max((total_minutes - lunch_in_work) * 60.0, 1.0)
    per_second = day_salary / billable_seconds

    # 进度：纯时钟比例（含午休），仅显示用
    span_seconds = (end - start).total_seconds()
    if span_seconds <= 0:
        progress = 1.0 if now >= start else 0.0
    else:
        progress = min(max((now - start).total_seconds() / span_seconds, 0.0), 1.0)

    if now < start:
        return BurnStats("before", 0.0, progress, day_salary,
                         per_second, per_second * 60, per_second * 3600)
    if now >= end:
        return BurnStats("after", day_salary, progress, day_salary,
                         per_second, per_second * 60, per_second * 3600)

    # 班内：已过时长剔除与午休的重叠
    elapsed_minutes = (now - start).total_seconds() / 60.0
    lunch_in_elapsed = _overlap_minutes(start, now, lunch_start, lunch_end)
    worked_seconds = max(elapsed_minutes - lunch_in_elapsed, 0.0) * 60.0
    earned = worked_seconds * per_second
    return BurnStats("during", earned, progress, day_salary,
                     per_second, per_second * 60, per_second * 3600)


# ---------------------------------------------------------------------------
# 发薪周期（进度条口径：每月 PAYDAY_DAY 号 18:00 发薪，逢非工作日前移）
# ---------------------------------------------------------------------------

# 每月发薪日：10 号 18:00（下班后发）。PAYDAY_DAY 那天不是工作日（周末/法定假日）
# 时整体「前移」到最近的前一个工作日，发薪时刻仍是 18:00。
PAYDAY_DAY = 10
PAYDAY_TIME = dtime(18, 0)


def _month_offset(y: int, mo: int, delta: int) -> tuple[int, int]:
    """(year, month) 顺移 delta 个月（delta 可负），返回新的 (year, month)。"""
    idx = y * 12 + (mo - 1) + delta
    return idx // 12, idx % 12 + 1


def payday_of(year: int, month: int, cfg: dict, holidays: dict) -> datetime:
    """某年某月的发薪时刻（含节假日前移）。

    基准 = 当月 PAYDAY_DAY 号 18:00；若当天不是工作日，逐日向前回退到最近的
    工作日（仍按 18:00 发）。holidays/调休口径与 eta 的 is_workday 一致。
    """
    d = date(year, month, PAYDAY_DAY)
    while not is_workday(d, cfg, holidays):
        d -= timedelta(days=1)
    return datetime.combine(d, PAYDAY_TIME)


def payday_cycle(now: datetime, cfg: dict, holidays: dict) -> tuple[datetime, datetime, float]:
    """当前所处的发薪周期。

    返回 (上次发薪时刻, 下次发薪时刻, 进度 0.0~1.0)。进度 = 距上次发薪已过时长 /
    整个周期时长，线性推进；到下次发薪瞬间（now == 下次发薪）进度归 0 重新开新周期
    ——「发薪即重置」。
    """
    y, mo = now.year, now.month
    # 下次发薪：从本月起向后找第一个严格 > now 的发薪时刻
    nxt = None
    for k in range(4):
        yy, mm = _month_offset(y, mo, k)
        p = payday_of(yy, mm, cfg, holidays)
        if p > now:
            nxt = p
            break
    if nxt is None:  # 兜底（极端输入也不抛）：取下月名义值
        yy, mm = _month_offset(y, mo, 1)
        nxt = datetime.combine(date(yy, mm, PAYDAY_DAY), PAYDAY_TIME)
    # 上次发薪：从本月起向前找最后一个 <= now 的发薪时刻
    last = None
    for k in range(3):
        yy, mm = _month_offset(y, mo, -k)
        p = payday_of(yy, mm, cfg, holidays)
        if p <= now:
            last = p
            break
    if last is None:  # 兜底同上
        last = nxt - timedelta(days=28)
    span = (nxt - last).total_seconds()
    ratio = 0.0 if span <= 0 else min(max((now - last).total_seconds() / span, 0.0), 1.0)
    return last, nxt, ratio


def _progress_bar(ratio: float, width: int = _BAR_WIDTH) -> str:
    filled = round(min(max(ratio, 0.0), 1.0) * width)
    return "█" * filled + "░" * (width - filled)


def build_frame(stats: BurnStats, now: datetime, cfg: dict) -> RenderableType:
    """组装一帧面板（整帧替换，不逐行打印）。"""
    header = Text(PANEL_TITLE, style="bold cyan")

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", justify="right", no_wrap=True)
    grid.add_column(justify="left", no_wrap=True)

    # 高亮大字：班内用亮绿，边界态用主色
    earned_style = "bold bright_green" if stats.state == "during" else "bold cyan"
    grid.add_row("今日已赚", Text(money(stats.earned), style=earned_style))

    grid.add_row(
        "费率",
        f"¥{stats.per_second:.4f}/秒 · ¥{stats.per_minute:.2f}/分 · ¥{stats.per_hour:.2f}/时",
    )
    grid.add_row(
        "今日进度",
        f"{_progress_bar(stats.progress)} {stats.progress * 100:.1f}%"
        f" ({cfg['work_start']} - {cfg['work_end']})",
    )
    grid.add_row("当前时刻", now.strftime("%H:%M:%S"))

    if stats.state == "before":
        grid.add_row("状态", Text("未到上班时间", style="yellow"))
    elif stats.state == "after":
        grid.add_row(
            "状态",
            Text(f"已下班 · 全天收入 {money(stats.day_salary)}", style="green"),
        )

    return Group(header, Text(), grid)


def main() -> None:
    cfg = load_config()
    set_console_title(WINDOW_TITLE)

    exit_after = auto_exit_seconds()
    deadline = time.monotonic() + exit_after if exit_after is not None else None

    try:
        with Live(refresh_per_second=4, screen=False) as live:
            while True:
                now = get_now()
                live.update(build_frame(compute_stats(cfg, now), now, cfg))
                if deadline is not None and time.monotonic() >= deadline:
                    break
                # 对齐到下一整秒；若有退出期限则不越过
                nap = 1.0 - (time.time() % 1.0)
                if deadline is not None:
                    nap = min(nap, max(deadline - time.monotonic(), 0.05))
                time.sleep(nap)
    except KeyboardInterrupt:
        pass
