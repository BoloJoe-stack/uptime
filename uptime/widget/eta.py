"""eta 桌面挂件——赛博 HUD 风。

纯黑底+荧光绿+四角取景框+扫描线：ETA 大倒计时（暗绿重影，无闪烁光标）+ 今日进度 + 下个假期。
计算复用 uptime.eta / uptime.burn 的纯函数，本模块只做皮肤。
"""

from __future__ import annotations

import math
import os
import random
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

FONT_BIG = ("Consolas", 22, "bold")              # 倒计时数字（等宽）
FONT_OFF = ("Microsoft YaHei UI", 48, "bold")    # 下班大字（中文，放大居中）
FONT_LABEL = ("Consolas", 10)
FONT_HOL = ("Consolas", 10, "bold")
FONT_PCT = ("Consolas", 10, "bold")

# 下班庆祝粒子亮色盘（每粒子独立随机色 → 色彩丰富）
_EDGE_PAL = (
    "#00FF00", "#7FFF00", "#E4FF57", "#FFD23F", "#FFA62B", "#FF7A00",
    "#FF4D4D", "#FF2E63", "#FF4FC3", "#D245FF", "#9B5CFF", "#6E7BFF",
    "#3D7BFF", "#00CFFF", "#3DE1FF", "#8AFFE3", "#FFFFFF",
)


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
        # 烟花触发状态：on→off（18:00 那一秒）放一次、同天不重复；
        # UPTIME_FIREWORKS_NOW=1 可强制演示一轮（不看时刻）
        self._prev_kind: str | None = None
        self._fired_day = None
        self._fw: Any | None = None
        self._off_view = False  # 下班视图：放大「下班」、隐藏进度/假日等其余元素
        self._fw_demo = os.environ.get("UPTIME_FIREWORKS_NOW") == "1"
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
        kind = status["kind"]
        prev = self._prev_kind
        self._prev_kind = kind
        # 下班瞬间（on→off，即 18:00:00 那一 tick）eta 显示「下班」+ 框内四边小烟花；
        # 同天只触发一次；UPTIME_FIREWORKS_NOW=1 可立即预览一次
        if self._fw_demo:
            self._fw_demo = False
            self._celebrate()
        elif prev == "on" and kind == "off" and self._fired_day != now.date():
            self._fired_day = now.date()
            self._celebrate()
        stats = compute_stats(self._cfg, now)  # 复用其今日进度比例

        # 下班视图切换：off 时放大「下班」居中并隐藏进度/假日等；回 on 复原
        if kind == "off" and not self._off_view:
            self._enter_off_view()
        elif kind != "off" and self._off_view:
            self._exit_off_view()

        if status["kind"] == "on":
            secs = int(status["remaining"].total_seconds())
            main = f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"
        elif status["kind"] == "off":
            main = "下　班"   # 全角空格拉开两字，更像分开的大字
        elif status["kind"] == "holiday":
            main = status.get("name", "HOLIDAY")[:9]
        else:
            main = "WEEKEND"

        # 主字与暗影层用同一份文案一起刷，绝不同帧错位（否则前一秒数字留残影）
        # 字体/居中在 _enter_off_view/_exit_off_view 切换，这里只刷文本
        c.itemconfigure("cd", text=main)
        c.itemconfigure("cd_ghost", text=main)

        ratio = max(0.0, min(1.0, stats.progress))
        c.coords("bar_fill", 19, 97, 19 + int(202 * ratio), 109)
        # 百分比向下取整：ratio==1.0（18:00:00 那一瞬）才显示 100%；
        # 四舍五入会在约 17:57 起（ratio>=0.995）就满格，过早到 100%
        c.itemconfigure("pct", text=f"{int(ratio * 100)}%")

        nh = next_holiday(now, self._holidays)
        if nh is None:
            c.itemconfigure("hol", text="HOL: no data")
        elif nh["kind"] == "next":
            # 倒计时终点 = 放假前最后一个工作日的下班时刻（下班那刻即算放假）
            target = holiday_break_start(nh["date"], self._cfg, self._holidays)
            c.itemconfigure("hol", text=f"HOL:{nh['name']} {_fmt_left(target, now)}")
        else:  # 今天正在假日中
            c.itemconfigure("hol", text=f"HOL: now {nh['name']}", fill=C_GREEN)

    # -- 下班视图：放大「下班」居中、藏起其余元素 ----------------------------
    def _enter_off_view(self) -> None:
        """下班态布局：隐藏进度条/百分比/假日行，把「下班」放大居中显示。"""
        if self._off_view:
            return
        self._off_view = True
        c = self.canvas
        for t in ("hol", "bar_bg", "bar_fill", "pct"):
            try:
                c.itemconfigure(t, state="hidden")
            except tk.TclError:
                pass
        for tag, dx, dy in (("cd", 0, 0), ("cd_ghost", -1, -1)):
            c.itemconfigure(tag, anchor="center", font=FONT_OFF)
            c.coords(tag, self.WIDTH // 2 + dx, 92 + dy)

    def _exit_off_view(self) -> None:
        """次日/回到上班态：恢复原布局（倒计时字体位置 + 进度/假日行）。"""
        if not self._off_view:
            return
        self._off_view = False
        c = self.canvas
        for t in ("hol", "bar_bg", "bar_fill", "pct"):
            try:
                c.itemconfigure(t, state="normal")
            except tk.TclError:
                pass
        c.itemconfigure("cd", anchor="w", font=FONT_BIG)
        c.itemconfigure("cd_ghost", anchor="w", font=FONT_BIG)
        c.coords("cd", 19, 63)
        c.coords("cd_ghost", 20, 64)

    # -- 下班庆祝：藏其他 uptime 视图 + 卡内四边多向彩烟花 ------------------
    def _celebrate(self) -> None:
        """下班瞬间：不放大、不挪动 eta，就在原小卡内——切到「下班」放大视图，
        在上/侧空域一朵朵「空中炸开」的小烟花，约 2.2s 后自行消隐。"""
        if getattr(self, "_cele_active", False):
            return
        self._cele_active = True
        self._cele_parts: list[list[float | int | str]] = []
        self._cele_frame = 0
        self._hide_other_windows()
        if not self._off_view:
            self._enter_off_view()          # 放大居中的「下班」视图
        self.after(40, self._cele_tick)

    def _hide_other_windows(self) -> None:
        """把其它 "uptime - xxx" 顶层窗口最小化到任务栏（不碰 eta 自己）。

        用 GetAncestor(GA_ROOT) 求自身真实顶层句柄来排除自己，避免 enum 到的是
        内部子句柄而把庆祝大卡自己也收掉。
        """
        try:
            import ctypes
            import win32con
            import win32gui

            self.update_idletasks()
            user32 = ctypes.windll.user32
            hw = int(self.winfo_id())
            own = {hw, int(user32.GetAncestor(hw, 2))}  # GA_ROOT=2

            def _cb(hwnd: int, _x) -> bool:
                try:
                    if hwnd not in own and win32gui.IsWindowVisible(hwnd):
                        t = (win32gui.GetWindowText(hwnd) or "").strip()
                        if t.startswith("uptime - ") and not win32gui.IsIconic(hwnd):
                            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
                except Exception:  # noqa: BLE001
                    pass
                return True

            win32gui.EnumWindows(_cb, None)
        except Exception:  # noqa: BLE001
            pass

    def _cele_spawn(self) -> None:
        """小卡内空中炸开：随机一个避开「下班」大字的空点，向 360° 小范围爆开。"""
        W, H = self.WIDTH, self.HEIGHT
        bx0, bx1, by0, by1 = 46.0, float(W - 46), 54.0, float(H - 6)     # 大字占区
        cx, cy = W / 2, 20.0
        for _ in range(12):
            x = random.uniform(6, W - 6)
            y = random.uniform(8, H - 14)
            if not (bx0 <= x <= bx1 and by0 <= y <= by1):
                cx, cy = x, y
                break
        for _ in range(random.randint(20, 32)):
            ang = random.uniform(0.0, math.tau)
            sp = random.uniform(0.6, 2.6)
            vx = math.cos(ang) * sp
            vy = math.sin(ang) * sp
            col = random.choice(_EDGE_PAL)
            if random.random() < 0.14:
                col = "#FFFFFF"
            self._cele_parts.append([cx, cy, vx, vy, random.randint(14, 26), col])

    def _cele_tick(self) -> None:
        if not getattr(self, "_cele_active", False):
            return
        try:
            c = self.canvas
            self._cele_frame += 1
            # 每 ~0.72s(18帧) 空中炸一发；总时长 30s（~750 帧），末尾 1s 停新发再清场
            if self._cele_frame <= 725 and (self._cele_frame - 1) % 18 == 0:
                self._cele_spawn()
            W, H = self.WIDTH, self.HEIGHT
            alive = []
            for p in self._cele_parts:
                x, y, vx, vy, life, col = p
                life -= 1
                if life <= 0:
                    continue
                vy += 0.08
                x += vx
                y += vy
                if x < -8 or x > W + 8 or y < -8 or y > H + 8:
                    continue
                alive.append([x, y, vx, vy, life, col])
            self._cele_parts = alive
            c.delete("cele_p")
            for x, y, _vx, _vy, life, col in self._cele_parts:
                r = 1.0 + 1.6 * min(1.0, life / 16.0)   # 随寿命缩小（淡出）
                c.create_oval(x - r, y - r, x + r, y + r, fill=col, outline=col, tags="cele_p")
            if self._cele_frame >= 750:
                self._cele_end()
                return
            self.after(40, self._cele_tick)
        except tk.TclError:
            self._cele_end()

    def _cele_end(self) -> None:
        """庆祝结束：只清粒子，保留已进入的「下班」放大视图。"""
        if not getattr(self, "_cele_active", False):
            return
        self._cele_active = False
        try:
            self.canvas.delete("cele_p")
        except tk.TclError:
            pass

