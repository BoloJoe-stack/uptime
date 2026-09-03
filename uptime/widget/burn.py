"""burn 桌面挂件——美元纸币风（Neubrutalism 骨架 + 纸币配色）。

米白纸底+黑边硬投影+金褐水印波点；金额（今日/本月/金币跳动）一律美钞绿墨，
默认打码、鼠标悬停显真值；每秒 +秒薪 绿字上飘（无爆框，纯数字）。
进度条口径 = 发薪周期：每月 10 号 18:00 发薪（逢周末/节假日提前到前一个工作日），
发薪瞬间进度归 0、整月线性爬向满格（下次发薪 = 100%），条下方配「距发薪 X天X时」倒计时。
计算复用 uptime.burn 的纯函数，本模块只做皮肤。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

import tkinter as tk

from uptime.burn import compute_stats, payday_cycle
from uptime.common.render import get_now
from uptime.eta import is_workday, load_holidays
from uptime.widget import WidgetBase, _dbg

# 美元纸币色板
C_BG = "#EDEDD8"      # 米白纸底
C_INK = "#111111"     # 雕版黑（边框/标签）
C_MONEY = "#1E7A34"   # 美钞绿墨（金额/进度条）
C_DOT = "#C9B458"     # 金褐水印波点
C_WHITE = "#FFFFFF"

FONT_NUM = ("Segoe UI", 21, "bold")
FONT_HEAD = ("Microsoft YaHei UI", 11, "bold")
FONT_MONTH = ("Microsoft YaHei UI", 11, "bold")
FONT_PCT = ("Segoe UI", 10, "bold")
FONT_COIN = ("Segoe UI", 11, "bold")
FONT_PAY = ("Microsoft YaHei UI", 10, "bold")  # 底部「距发薪」倒计时行


def _earned_month(cfg: dict[str, Any], today_earned: float, now: datetime, holidays: dict) -> float:
    """本月已赚 = 已过工作日×日薪 + 今日已赚（调休感知）。"""
    daily = cfg["monthly_salary"] / cfg["monthly_workdays"]
    total = 0.0
    d = now.date().replace(day=1)
    while d < now.date():
        if is_workday(d, cfg, holidays):
            total += daily
        d += timedelta(days=1)
    if is_workday(now.date(), cfg, holidays):
        total += today_earned
    return total


class BurnWidget(WidgetBase):
    CODE = "burn"
    WIDTH = 280
    HEIGHT = 150

    def __init__(self, root, cfg, on_close: Callable[[str], None] | None = None) -> None:
        try:
            self._holidays = load_holidays()
        except Exception:  # noqa: BLE001 —— 假日数据异常按无假日处理
            self._holidays = {"off_days": {}, "extra_workdays": []}
        self._tick_no = 0
        super().__init__(root, cfg, on_close)

    # -- 首次绘制 ----------------------------------------------------------
    def _render(self) -> None:
        c = self.canvas
        W, H = self.WIDTH, self.HEIGHT
        M = W - 5  # 主卡右/下边界（右下留 5px 给硬投影）
        # 硬投影 + 主卡 + 粗黑边
        c.create_rectangle(5, 5, W, H, fill=C_INK, outline=C_INK)
        c.create_rectangle(0, 0, M, H - 5, fill=C_BG, outline=C_INK, width=4)
        # 波点（水印感金褐 dots）：头部中部一小片，避开 −/× 按钮
        for gy in range(18, 45, 9):
            for gx in range(M - 156, M - 66, 9):
                c.create_oval(gx, gy, gx + 3, gy + 3, fill=C_DOT, outline=C_DOT)
        # 头部
        c.create_text(16, 28, anchor="w", text="今日已赚", font=FONT_HEAD, fill=C_INK)
        # − 最小化钮 + × 关闭钮（黑块白字，独立 tag 拦截拖动）
        self._add_min_button(c, M - 58, 10, M - 36, 32,
                             fill=C_INK, outline=C_INK, glyph_fill=C_WHITE)
        c.create_rectangle(M - 32, 10, M - 10, 32, fill=C_INK, outline=C_INK, tags="closebox")
        c.create_text(M - 21, 21, text="✕", font=("Segoe UI", 11, "bold"), fill=C_WHITE, tags="close")
        c.tag_bind("closebox", "<Button-1>", lambda e: "break")
        c.tag_bind("close", "<Button-1>", lambda e: "break")
        c.tag_bind("closebox", "<ButtonRelease-1>", lambda e: self.close())
        c.tag_bind("close", "<ButtonRelease-1>", lambda e: self.close())
        # 金额（黑色描边感：四向偏移黑字垫底 + 红字封面）
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            c.create_text(16 + dx, 66 + dy, anchor="w", text="¥ --.--",
                          font=FONT_NUM, fill=C_INK, tags="num_sh")
        c.create_text(16, 66, anchor="w", text="¥ --.--", font=FONT_NUM,
                      fill=C_MONEY, tags="num")
        # 本月行
        c.create_text(16, 96, anchor="w", text="本月 ¥ --,--", font=FONT_MONTH,
                      fill=C_MONEY, tags="month")
        # 进度条（黑框：白底+绿墨填充；口径 = 发薪周期，见 _update）
        c.create_rectangle(14, 112, 218, 126, fill=C_WHITE, outline=C_INK, width=2, tags="bar_bg")
        c.create_rectangle(15, 113, 15, 125, fill=C_MONEY, outline=C_MONEY, tags="bar_fill")
        c.create_text(232, 119, anchor="w", text="--%", font=FONT_PCT, fill=C_INK, tags="pct")
        # 底部「距发薪」倒计时行
        c.create_text(16, 140, anchor="w", text="距发薪 --", font=FONT_PAY,
                      fill=C_MONEY, tags="paytxt")
        self._update()

    # -- 每秒刷新 ----------------------------------------------------------
    def _update(self) -> None:
        c = self.canvas
        now = get_now()
        stats = compute_stats(self._cfg, now)
        self._tick_no += 1

        shown = f"¥ {stats.earned:,.2f}" if self._hover else "¥ --.--"
        c.itemconfigure("num", text=shown)
        for item in c.find_withtag("num_sh"):
            c.itemconfigure(item, text=shown)

        month = _earned_month(self._cfg, stats.earned, now, self._holidays)
        c.itemconfigure("month",
                        text=f"本月 ¥ {month:,.0f}" if self._hover else "本月 ¥ --,--")

        # 进度条 = 发薪周期进度：上次发薪 0% 线性爬向下次发薪满格，发薪瞬间归 0 重开
        _last_pay, next_pay, cycle = payday_cycle(now, self._cfg, self._holidays)
        ratio = max(0.0, min(1.0, cycle))
        c.coords("bar_fill", 15, 113, 15 + int(202 * ratio), 125)
        c.itemconfigure("pct", text=f"{ratio * 100:.0f}%")
        c.itemconfigure("paytxt", text=self._fmt_pay_left(next_pay, now))

        if stats.state == "during":
            self._coin_anim(stats.per_second)

    @staticmethod
    def _fmt_pay_left(next_pay: datetime, now: datetime) -> str:
        """下次发薪剩余时长 → 「距发薪 X天X时 / X时X分 / X分」。"""
        left = next_pay - now
        secs = int(left.total_seconds())
        if secs <= 0:
            return "发薪日"
        days, rem = divmod(secs, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        if days >= 1:
            return f"距发薪 {days}天{hours:02d}时"
        if hours >= 1:
            return f"距发薪 {hours}时{minutes:02d}分"
        return f"距发薪 {minutes}分"

    # -- 金币跳动动画 --------------------------------------------------------
    def _coin_anim(self, per_second: float) -> None:
        """+秒薪 美钞绿字上飘淡出（纯数字，无爆框）。"""
        c = self.canvas
        c.delete("coin")
        c.create_text(206, 92, text=f"+{per_second:.2f}", font=FONT_COIN,
                      fill=C_MONEY, tags="coin")

        def _step(n: int) -> None:
            if n > 7:
                c.delete("coin")
                return
            c.move("coin", 0, -3)
            self.after(75, lambda: _step(n + 1))

        self.after(75, lambda: _step(0))
