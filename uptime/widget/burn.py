"""burn 桌面挂件——美式动漫风（Neubrutalism/波普）。

黄底粗黑边+波点+硬投影；金额默认打码，鼠标悬停显真值（今日已赚+本月已赚）；
每秒金币跳动动画（+秒薪 爆炸框上飘，逢十 KA-CHING! 彩蛋）。
计算复用 uptime.burn 的纯函数，本模块只做皮肤。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Callable

import tkinter as tk

from uptime.burn import compute_stats
from uptime.eta import is_workday, load_holidays
from uptime.widget import WidgetBase, _dbg

# 漫画波普色板（Neubrutalism）
C_BG = "#FFEB3B"      # 高饱和黄底
C_INK = "#111111"     # 粗黑描边/文字
C_RED = "#FF5252"     # 大红数字
C_BLUE = "#2196F3"    # 进度蓝
C_WHITE = "#FFFFFF"

FONT_NUM = ("Segoe UI", 21, "bold")
FONT_HEAD = ("Microsoft YaHei UI", 11, "bold")
FONT_MONTH = ("Microsoft YaHei UI", 11, "bold")
FONT_PCT = ("Segoe UI", 10, "bold")
FONT_COIN = ("Segoe UI", 10, "bold")
FONT_KA = ("Impact", 11)


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
        # 波点（Ben-Day dots）：头部中部一小片，避开×按钮
        for gy in range(18, 45, 9):
            for gx in range(M - 130, M - 48, 9):
                c.create_oval(gx, gy, gx + 3, gy + 3, fill=C_RED, outline=C_RED)
        # 头部
        c.create_text(16, 28, anchor="w", text="今日已赚", font=FONT_HEAD, fill=C_INK)
        # × 关闭钮（黑块白×，独立 tag 拦截拖动）
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
                      fill=C_RED, tags="num")
        # 本月行
        c.create_text(16, 96, anchor="w", text="本月 ¥ --,--", font=FONT_MONTH,
                      fill=C_INK, tags="month")
        # 进度条（黑框：白底+蓝色填充，未填充部分对比清晰）
        c.create_rectangle(14, 112, 218, 126, fill=C_WHITE, outline=C_INK, width=2, tags="bar_bg")
        c.create_rectangle(15, 113, 15, 125, fill=C_BLUE, outline=C_BLUE, tags="bar_fill")
        c.create_text(232, 119, anchor="w", text="--%", font=FONT_PCT, fill=C_INK, tags="pct")
        self._update()

    # -- 每秒刷新 ----------------------------------------------------------
    def _update(self) -> None:
        c = self.canvas
        now = datetime.now()
        stats = compute_stats(self._cfg, now)
        self._tick_no += 1

        shown = f"¥ {stats.earned:,.2f}" if self._hover else "¥ --.--"
        c.itemconfigure("num", text=shown)
        for item in c.find_withtag("num_sh"):
            c.itemconfigure(item, text=shown)

        month = _earned_month(self._cfg, stats.earned, now, self._holidays)
        c.itemconfigure("month",
                        text=f"本月 ¥ {month:,.0f}" if self._hover else "本月 ¥ --,--")

        ratio = max(0.0, min(1.0, stats.progress))
        c.coords("bar_fill", 15, 113, 15 + int(202 * ratio), 125)
        c.itemconfigure("pct", text=f"{ratio * 100:.0f}%")

        if stats.state == "during":
            self._coin_anim(stats.per_second)

    # -- 金币跳动动画 --------------------------------------------------------
    def _coin_anim(self, per_second: float) -> None:
        """+秒薪 爆炸框上飘淡出；逢 10 秒一次 KA-CHING! 彩蛋。"""
        c = self.canvas
        c.delete("coin")
        star = self._star(206, 92, 34, 12)
        c.create_polygon(star, fill=C_WHITE, outline=C_INK, width=2, tags="coin")
        txt = f"+{per_second:.2f}" if self._tick_no % 10 else "KA-CHING!"
        c.create_text(206, 92, text=txt,
                      font=FONT_KA if txt.startswith("K") else FONT_COIN,
                      fill=C_INK, tags="coin")

        def _step(n: int) -> None:
            if n > 7:
                c.delete("coin")
                return
            c.move("coin", 0, -3)
            self.after(75, lambda: _step(n + 1))

        self.after(75, lambda: _step(0))

    @staticmethod
    def _star(cx: int, cy: int, r: int, n: int = 12) -> list[tuple[float, float]]:
        """爆炸框星形顶点。"""
        pts = []
        for i in range(n * 2):
            ang = math.pi * i / n - math.pi / 2
            rad = r if i % 2 == 0 else r * 0.55
            pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
        return pts
