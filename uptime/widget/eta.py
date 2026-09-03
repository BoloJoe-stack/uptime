"""eta 桌面挂件——赛博 HUD 风。

纯黑底+荧光绿+四角取景框+扫描线：ETA 大倒计时（暗绿重影，无闪烁光标）+ 今日进度 + 下个假期。
计算复用 uptime.eta / uptime.burn 的纯函数，本模块只做皮肤。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

import tkinter as tk

from uptime.burn import compute_stats
from uptime.common.render import get_now
from uptime.eta import day_status, holiday_break_start, load_holidays, next_holiday
from uptime.widget import WidgetBase

# 赛博 HUD 色板
C_BG = "#0D0D0D"       # 纯黑
C_GREEN = "#00FF00"    # Matrix 绿
C_DIM = "#17A317"      # 暗绿（辅助字，扫描线上可读）
C_SCAN = "#123812"     # 扫描线
C_WHITE = "#FFFFFF"

FONT_BIG = ("Consolas", 22, "bold")
FONT_LABEL = ("Consolas", 10)
FONT_HOL = ("Consolas", 10, "bold")
FONT_PCT = ("Consolas", 10, "bold")


def _fmt_left(target: datetime, now: datetime) -> str:
    """到目标的剩余时长（精确到秒）：>=1天 → "Xd HH:MM:SS"，当天 → "HH:MM:SS"。"""
    secs = int((target - now).total_seconds())
    if secs <= 0:
        return "00:00:00"
    days, rem = divmod(secs, 86400)
    hh, rem = divmod(rem, 3600)
    mm, ss = divmod(rem, 60)
    if days >= 1:
        return f"{days}d {hh:02d}:{mm:02d}:{ss:02d}"
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


class EtaWidget(WidgetBase):
    CODE = "eta"
    WIDTH = 280
    HEIGHT = 150

    def __init__(self, root, cfg, on_close: Callable[[str], None] | None = None) -> None:
        try:
            self._holidays = load_holidays()
        except Exception:  # noqa: BLE001
            self._holidays = {"off_days": {}, "extra_workdays": []}
        super().__init__(root, cfg, on_close)

    # -- 首次绘制 ----------------------------------------------------------
    def _render(self) -> None:
        c = self.canvas
        W, H = self.WIDTH, self.HEIGHT
        c.configure(bg=C_BG)
        c.create_rectangle(0, 0, W, H, fill=C_BG, outline=C_BG)  # 铺底
        # 扫描线（内容下层）
        for y in range(0, H, 4):
            c.create_line(0, y, W, y, fill=C_SCAN, tags="scan")
        # 四角取景框
        a, t = 8, 14
        for (x1, y1, dx, dy) in ((a, a, 1, 1), (W - a, a, -1, 1), (a, H - a, 1, -1), (W - a, H - a, -1, -1)):
            c.create_line(x1, y1, x1 + dx * t, y1, fill=C_GREEN, width=2)
            c.create_line(x1, y1, x1, y1 + dy * t, fill=C_GREEN, width=2)
        # 头部
        c.create_text(20, 26, anchor="w", text="ETA", font=FONT_LABEL, fill=C_DIM)
        self._add_min_button(c, W - 60, 12, W - 38, 34,
                             fill=C_BG, outline=C_DIM, glyph_fill=C_GREEN)
        c.create_rectangle(W - 34, 12, W - 12, 34, outline=C_DIM, width=2, tags="closebox")
        c.create_text(W - 23, 23, text="✕", font=("Segoe UI", 10, "bold"),
                      fill=C_GREEN, tags="close")
        c.tag_bind("closebox", "<Button-1>", lambda e: "break")
        c.tag_bind("close", "<Button-1>", lambda e: "break")
        c.tag_bind("closebox", "<ButtonRelease-1>", lambda e: self.close())
        c.tag_bind("close", "<ButtonRelease-1>", lambda e: self.close())
        # 大倒计时（暗绿重影 + 荧光绿封面；两层每次同文案一起刷新，避免错层残影）
        c.create_text(20, 64, anchor="w", text="--:--:--", font=FONT_BIG,
                      fill=C_DIM, tags="cd_ghost")
        c.create_text(19, 63, anchor="w", text="--:--:--", font=FONT_BIG,
                      fill=C_GREEN, tags="cd")
        # 进度条（暗绿框 + 荧光绿块）
        c.create_rectangle(18, 96, 222, 110, outline=C_DIM, width=2, tags="bar_bg")
        c.create_rectangle(19, 97, 19, 109, fill=C_GREEN, outline=C_GREEN, tags="bar_fill")
        c.create_text(236, 103, anchor="w", text="--%", font=FONT_PCT, fill=C_GREEN, tags="pct")
        # 假日行
        c.create_text(20, 130, anchor="w", text="HOL: --", font=FONT_HOL,
                      fill=C_DIM, tags="hol")
        self._update()

    # -- 每秒刷新 ----------------------------------------------------------
    def _update(self) -> None:
        c = self.canvas
        now = get_now()
        status = day_status(now, self._cfg, self._holidays)
        stats = compute_stats(self._cfg, now)  # 复用其今日进度比例

        if status["kind"] == "on":
            secs = int(status["remaining"].total_seconds())
            main = f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"
        elif status["kind"] == "off":
            main = "OFF-DUTY"
        elif status["kind"] == "holiday":
            main = status.get("name", "HOLIDAY")[:9]
        else:
            main = "WEEKEND"

        # 主字与暗影层用同一份文案一起刷，绝不同帧错位（否则前一秒数字留残影）
        c.itemconfigure("cd", text=main)
        c.itemconfigure("cd_ghost", text=main)

        ratio = max(0.0, min(1.0, stats.progress))
        c.coords("bar_fill", 19, 97, 19 + int(202 * ratio), 109)
        c.itemconfigure("pct", text=f"{ratio * 100:.0f}%")

        nh = next_holiday(now, self._holidays)
        if nh is None:
            c.itemconfigure("hol", text="HOL: no data")
        elif nh["kind"] == "next":
            # 倒计时终点 = 放假前最后一个工作日的下班时刻（下班那刻即算放假）
            target = holiday_break_start(nh["date"], self._cfg, self._holidays)
            c.itemconfigure("hol", text=f"HOL:{nh['name']} {_fmt_left(target, now)}")
        else:  # 今天正在假日中
            c.itemconfigure("hol", text=f"HOL: now {nh['name']}", fill=C_GREEN)

    # -- 对齐整秒的刷新调度 --------------------------------------------------
    def _tick(self) -> None:
        """每秒刷新，并把下一次安排在**真实整秒边界**之后几毫秒。

        用 after(1000) 会随调用开销累积漂移、且不与秒边界对齐，导致倒计时
        跳的瞬间错位、观感上不完全是「一秒一跳」。
        """
        try:
            self._update()
        except Exception:  # noqa: BLE001 —— 刷新异常不终止循环
            pass
        try:
            self.after(self._ms_to_next_second(), self._tick)
        except tk.TclError:
            pass

    @staticmethod
    def _ms_to_next_second() -> int:
        """距下一个整秒还有多少毫秒（多留 5ms 余量，确保已翻到新秒）。"""
        ms = int(time.time() * 1000) % 1000
        return max(1000 - ms + 5, 5)

