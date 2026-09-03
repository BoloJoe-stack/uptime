"""burn 桌面挂件——美元纸币风（Neubrutalism 骨架 + 纸币配色）。

米白纸底+黑边硬投影+金褐水印波点；金额（今日/本月）一律美钞绿墨，默认打码、鼠标悬停显真值。
两处小动画：①上班中每秒在金额右侧（原 +秒薪 位置）弹一枚小金币，飘起即淡没；
②「今日已赚」金额每跨过整元（如 12.34→13.00）时，从数字右上方抛一小把美钞小方块。
进度条口径 = 发薪周期：每月 10 号 18:00 发薪（逢周末/节假日提前到前一个工作日），
发薪瞬间进度归 0、整月线性爬向满格（下次发薪 = 100%），条下方配「距发薪 X天X时」倒计时。
计算复用 uptime.burn 的纯函数，本模块只做皮肤。
"""

from __future__ import annotations

import random
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
C_BILL = "#2E9E46"    # 弹起的美钞（更亮一档绿，视觉主角）
C_BILL_GOLD = "#C9B458"  # 偶发一张金褐钞票点缀（与水印波点同色系）
C_DOT = "#C9B458"     # 金褐水印波点
C_COIN_EDGE = "#B8860B"  # 金币外圈（深金）
C_COIN = "#F5C542"       # 金币币面（金）
C_COIN_LIT = "#FFE28A"   # 金币高光（浅金）
C_WHITE = "#FFFFFF"

FONT_NUM = ("Segoe UI", 21, "bold")
FONT_HEAD = ("Microsoft YaHei UI", 11, "bold")
FONT_MONTH = ("Microsoft YaHei UI", 11, "bold")
FONT_PCT = ("Segoe UI", 10, "bold")
FONT_PAY = ("Microsoft YaHei UI", 10, "bold")  # 底部「距发薪」倒计时行


def _mix_hex(a: str, b: str, t: float) -> str:
    """#RRGGBB 从 a→b 线性插值，t∈[0,1]（美钞淡出用）。"""
    t = min(max(t, 0.0), 1.0)
    rgb = []
    for i in (1, 3, 5):
        av = int(a[i:i + 2], 16)
        bv = int(b[i:i + 2], 16)
        rgb.append(round(av + (bv - av) * t))
    return "#%02X%02X%02X" % tuple(rgb)


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
        self._last_floor: int | None = None  # 上次今日已赚的整元（进位才抛美钞）
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

        # 动画：金币 = 上班中每秒在右侧弹一枚（取代原 +秒薪 数字，纯装饰不带数值）；
        #       美钞 = 今日金额每跨一个整元（如 12.34→13.00）时，从数字右上方抛一小把
        floor = int(stats.earned)
        if stats.state == "during":
            self._coin_pop()
            if self._last_floor is not None and floor > self._last_floor:
                self._bill_burst()
        self._last_floor = floor

        real = f"¥ {stats.earned:,.2f}"
        shown = real if self._hover else "¥ --.--"
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

    # -- 金币：每秒弹一枚 ----------------------------------------------------
    def _coin_pop(self) -> None:
        """上班中每秒在金额右侧弹一枚小金币：飘起一小段随即淡没（无数字，取代原 +秒薪）。"""
        c = self.canvas
        tag = f"coin{self._tick_no}_{random.randrange(1000)}"
        cx, cy = 210.0, 96.0               # 金额右侧空纸区（原 +秒薪 弹字处附近）
        vx = random.uniform(-0.6, 0.8)
        vy = random.uniform(-2.3, -1.7)

        def _step(k: int) -> None:
            if k > 7:
                try:
                    c.delete(tag)
                except tk.TclError:
                    pass
                return
            try:
                c.delete(tag)
            except tk.TclError:
                return
            x, y = cx + vx * k, cy + vy * k
            fade = (k + 1) / 8.0
            c.create_oval(x - 5, y - 5, x + 5, y + 5,
                          fill=_mix_hex(C_COIN, C_BG, fade),
                          outline=_mix_hex(C_COIN_EDGE, C_BG, fade), width=1, tags=tag)
            c.create_oval(x - 3, y - 3, x + 3, y + 3,
                          outline=_mix_hex(C_COIN_LIT, C_BG, fade), tags=tag)
            try:
                self.after(40, lambda: _step(k + 1))
            except tk.TclError:
                return

        self.after(40, lambda: _step(0))

    # -- 美钞：金额跨整元时抛一把 ----------------------------------------------
    def _bill_burst(self) -> None:
        """今日金额跨过一个整元时，在数字右上方抛一小把美钞小方块（弹起随即淡出）。"""
        c = self.canvas
        bb = c.bbox("num")
        if bb is not None:
            ox, oy = bb[2] + 4, bb[1] + 9   # 从数字右上角起抛
        else:
            ox, oy = 128, 58
        ox = min(ox, 186)                  # 别甩到右上 −/× 按钮区
        oy = min(max(oy, 44), 66)          # 别顶到头部/压到下方本月行
        tag = f"cash{self._tick_no}_{random.randrange(1000)}"
        bills: list[tuple[float, float, float, float, float, float, str]] = []
        for _ in range(6):
            w = random.uniform(8, 13)
            h = random.uniform(4, 6)
            x0 = ox + random.uniform(-5, 9)
            y0 = oy + random.uniform(-3, 4)
            vx = random.uniform(-1.4, 1.5)
            vy = random.uniform(-6.6, -4.2)
            r = random.random()
            col = C_MONEY if r < 0.6 else (C_BILL if r < 0.85 else C_BILL_GOLD)
            bills.append((w, h, x0, y0, vx, vy, col))

        def _step(k: int) -> None:
            if k > 9:
                try:
                    c.delete(tag)
                except tk.TclError:
                    pass
                return
            try:
                c.delete(tag)
            except tk.TclError:
                return
            fade = (k + 1) / 10.0
            for (w, h, x0, y0, vx, vy, col) in bills:
                x = x0 + vx * k
                y = y0 + vy * k + 0.45 * k * k     # 上抛抛物线，弹到最高点前淡没
                fill = _mix_hex(col, C_BG, fade)
                outline = _mix_hex(_mix_hex(col, C_INK, 0.5), C_BG, fade)
                c.create_rectangle(x, y, x + w, y + h, fill=fill, outline=outline,
                                   width=1, tags=tag)
            try:
                self.after(45, lambda: _step(k + 1))
            except tk.TclError:
                return

        self.after(45, lambda: _step(0))
