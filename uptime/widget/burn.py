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

from PIL import Image, ImageTk

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
C_WHITE = "#FFFFFF"

# 泰拉瑞亚金币（Gold Coin）像素贴图：以官方 12×16 图标逐像素复刻
# （terraria.wiki.gg Gold Coin，透明底、7 色），缩放 COIN_SCALE× 画出。
COIN_SCALE = 1   # 用户嫌大缩到 1 倍原尺寸（原 2 → 线性减半）
_COIN_PAL = {
    "A": "#5C4308", "B": "#CCB548", "C": "#FFF9B7", "D": "#4C2D08",
    "E": "#947E18", "F": "#EEDA7A", "G": "#7A5C0A",
}
_COIN_ROWS = [
    "    AAAA    ",
    "    AAAA    ",
    "  AABBCCAA  ",
    "  AABBCCAA  ",
    "DDBBEEBBCCAA",
    "DDBBEEBBCCAA",
    "DDBBEECCFFAA",
    "DDBBEECCFFAA",
    "DDEEGGFFBBDD",
    "DDEEGGFFBBDD",
    "DDEEGGGGBBDD",
    "DDEEGGGGBBDD",
    "  DDEEBBDD  ",
    "  DDEEBBDD  ",
    "    DDDD    ",
    "    DDDD    ",
]

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


def _coin_fade_frames() -> list[Image.Image]:
    """泰拉瑞亚金币贴图 → 8 帧 RGBA 淡出图（1/8…8/8 渐隐到纸底色）。

    每帧在透明底上按 COIN_SCALE 逐像素铺色；运行时用 ImageTk 一次性贴为单图片，
    动画只换图+移动（不做逐方块变色，避免太重）。
    """
    s = COIN_SCALE
    w = len(_COIN_ROWS[0]) * s
    h = len(_COIN_ROWS) * s
    frames: list[Image.Image] = []
    for step in range(1, 9):
        t = step / 8.0
        im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        px = im.load()
        for r, line in enumerate(_COIN_ROWS):
            for col, ch in enumerate(line):
                if ch == " ":
                    continue
                colc = _mix_hex(_COIN_PAL[ch], C_BG, t)
                rgb = (int(colc[1:3], 16), int(colc[3:5], 16), int(colc[5:7], 16))
                for dy in range(s):
                    for dx in range(s):
                        px[col * s + dx, r * s + dy] = (*rgb, 255)
        frames.append(im)
    return frames


_COIN_PIL_FRAMES = _coin_fade_frames()


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
    HEIGHT = 160   # 底部要放整行「距发薪」文字（含字体行框），150 会被框截断下半

    def __init__(self, root, cfg, on_close: Callable[[str], None] | None = None) -> None:
        try:
            self._holidays = load_holidays()
        except Exception:  # noqa: BLE001 —— 假日数据异常按无假日处理
            self._holidays = {"off_days": {}, "extra_workdays": []}
        self._tick_no = 0
        self._last_yuan: int | None = None  # 上次显示金额的整元（个位数+1 才抛美钞）
        self._coin_photos: dict[str, list[ImageTk.PhotoImage]] = {}  # 金币图引用防回收
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
        #       美钞 = 显示金额的个位数 +1（如 ¥12.99→¥13.00）那一帧就抛，不滞后——
        #       金额是四舍五入到分显示的，整元要用 round(元×100) 而不是 int(元) 截断
        yuan = int(round(stats.earned * 100)) // 100
        if stats.state == "during":
            self._coin_pop()
            if self._last_yuan is not None and yuan > self._last_yuan:
                self._bill_burst()
        self._last_yuan = yuan

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
        """下次发薪剩余时长 → 「距发薪 X天 HH:MM:SS」（精确到秒，每秒跳动）。"""
        left = next_pay - now
        secs = int(left.total_seconds())
        if secs <= 0:
            return "发薪日!"
        days, rem = divmod(secs, 86400)
        hh, rem = divmod(rem, 3600)
        mm, ss = divmod(rem, 60)
        if days >= 1:
            return f"距发薪 {days}天 {hh:02d}:{mm:02d}:{ss:02d}"
        return f"距发薪 {hh:02d}:{mm:02d}:{ss:02d}"

    # -- 金币：每秒弹一枚（泰拉瑞亚风像素金币）-----------------------------
    def _coin_pop(self) -> None:
        """上班中每秒在金额右侧抛一枚像素金币（复刻泰拉瑞亚 Gold Coin）：飘起淡没。

        用整张贴图（单图片项）+ 预生成 8 张淡出帧，每帧只换图+移动，O(1) 轻量。
        """
        c = self.canvas
        tag = f"coin{self._tick_no}_{random.randrange(1000)}"
        cx, cy0 = 210.0, 88.0              # 金额右侧空纸区（进度条上方，避开本月/美钞）
        vx = random.uniform(-0.4, 0.5)     # 每帧轻微水平飘
        vy = -1.6                          # 每帧上飘
        photos = [ImageTk.PhotoImage(f) for f in _COIN_PIL_FRAMES]
        self._coin_photos[tag] = photos    # 保住引用，防 Tk 回收
        item = c.create_image(cx, cy0, image=photos[0], tags=tag)

        def _step(k: int) -> None:
            if k > 7:
                try:
                    c.delete(tag)
                except tk.TclError:
                    pass
                self._coin_photos.pop(tag, None)
                return
            try:
                c.itemconfigure(item, image=photos[k])
                c.move(tag, vx, vy)
            except tk.TclError:
                self._coin_photos.pop(tag, None)
                return
            try:
                self.after(40, lambda: _step(k + 1))
            except tk.TclError:
                self._coin_photos.pop(tag, None)

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
