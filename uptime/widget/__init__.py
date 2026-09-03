"""桌面挂件底座：无边框置顶、整窗拖动、位置记忆、×关闭、悬停事件。

挂件与面板同进程（Toplevel 挂在面板的 Tk 根上）——不走子进程，
规避 cmd/PyInstaller 引导层一整类问题。窗口标题保持 "uptime - <代号>"，
隐身按钮按标题前缀枚举的既有逻辑天然覆盖。
"""

from __future__ import annotations

import tkinter as tk
from typing import Any, Callable

from uptime.common.config import config_path, load_config, save_config


def _dbg(msg: str) -> None:  # 与 panel/console 同一份诊断日志
    try:
        import os
        import tempfile
        import time
        from pathlib import Path

        p = Path(tempfile.gettempdir()) / "uptime_panel_dbg.log"
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} [widget] {msg}\n")
    except Exception:  # noqa: BLE001
        pass


class WidgetBase(tk.Toplevel):
    """子类需实现 _render()（首次全量绘制）与 _update()（每秒刷新）。"""

    CODE = ""          # 子类填：模块代号（窗口标题/配置键用）
    WIDTH = 280
    HEIGHT = 150

    def __init__(
        self,
        root: tk.Tk,
        cfg: dict[str, Any],
        on_close: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(root)
        # 面板传来的可能是 console 段（缺月薪等键）——挂件需要完整配置，自行补载
        if not (isinstance(cfg, dict) and "monthly_salary" in cfg):
            try:
                cfg = load_config()
            except Exception:  # noqa: BLE001 —— 配置异常也允许起窗（数字打码兜底）
                cfg = dict(cfg)
        self._cfg = cfg
        self._on_close = on_close
        self._hover = False
        self._drag_off = (0, 0)
        self.title(f"uptime - {self.CODE}")
        self.overrideredirect(True)
        self.attributes("-topmost", self._topmost())
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}")
        # 无边框窗映射前/映射中设坐标会被 WM 覆盖：绑 <Map> 真正上屏时落位（一次性）
        self._pos_applied = False
        self.bind("<Map>", self._on_map)

        self.config(bg="#000000")  # 画布铺满，黑色仅兜底
        self.canvas = tk.Canvas(
            self, width=self.WIDTH, height=self.HEIGHT,
            highlightthickness=0, bd=0, bg="#000000",
        )
        self.canvas.pack(fill="both", expand=True)

        # 拖动：整窗；×按钮单独 tag 拦截（return "break" 不进拖动）
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Button-1>", self._on_press)      # 边缘 1px 兜底
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.protocol("WM_DELETE_WINDOW", self.close)

        self._render()
        self.after(1000, self._tick)
        _dbg(f"{self.CODE} widget created")

    def _on_map(self, _event) -> None:
        if self._pos_applied:
            return
        self._pos_applied = True
        self.unbind("<Map>")
        _dbg(f"{self.CODE} mapped, applying pos")
        self._apply_saved_pos()
        _dbg(f"{self.CODE} pos now {self.winfo_x()},{self.winfo_y()}")

    def _add_min_button(self, canvas: tk.Canvas, x1: int, y1: int, x2: int, y2: int,
                        *, fill: str, outline: str, glyph_fill: str) -> None:
        """最小化（−）按钮：收起挂件；再点面板卡片/托盘项恢复（withdrawn 仍算运行中）。"""
        canvas.create_rectangle(x1, y1, x2, y2, fill=fill, outline=outline,
                                width=2, tags="minbox")
        canvas.create_text((x1 + x2) // 2, (y1 + y2) // 2 + 1, text="—",
                           font=("Segoe UI", 10, "bold"), fill=glyph_fill, tags="min")
        for t in ("minbox", "min"):
            canvas.tag_bind(t, "<Button-1>", lambda e: "break")
            canvas.tag_bind(t, "<ButtonRelease-1>", lambda e: self.minimize())

    def minimize(self) -> None:
        """收起挂件（withdraw）；恢复走 toggle_widget→show。"""
        _dbg(f"{self.CODE} minimized")
        self.hide()

    # -- 子类接口 ----------------------------------------------------------
    def _render(self) -> None:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def _update(self) -> None:  # pragma: no cover - 子类实现
        raise NotImplementedError

    # -- 公共行为 ----------------------------------------------------------
    def _topmost(self) -> bool:
        sec = self._cfg.get("widgets")
        if isinstance(sec, dict):
            return bool(sec.get("always_on_top", True))
        return True

    def _set_hover(self, on: bool) -> None:
        if self._hover != on:
            self._hover = on
            self._update()

    def _tick(self) -> None:
        try:
            self._update()
        except Exception:  # noqa: BLE001 —— 刷新异常不终止循环
            pass
        try:
            self.after(1000, self._tick)
        except tk.TclError:
            pass

    def show(self) -> None:
        """显示并置前（菜单/卡片二次点击/隐身恢复路径）。"""
        try:
            if self.state() == "withdrawn":
                self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
        except tk.TclError:
            pass

    def hide(self) -> None:
        try:
            self.withdraw()
        except tk.TclError:
            pass

    def close(self) -> None:
        """×按钮 / 卡片"结束"统一出口：销毁窗口并通知面板。"""
        _dbg(f"{self.CODE} widget close")
        try:
            self.destroy()
        except tk.TclError:
            pass
        if self._on_close is not None:
            self._on_close(self.CODE)

    def alive(self) -> bool:
        try:
            return bool(self.winfo_exists())
        except tk.TclError:
            return False

    # -- 拖动与位置记忆 ------------------------------------------------------
    def _on_press(self, event) -> None:
        self._drag_off = (event.x, event.y)

    def _on_drag(self, event) -> None:
        x = self.winfo_pointerx() - self._drag_off[0]
        y = self.winfo_pointery() - self._drag_off[1]
        self.geometry(f"+{x}+{y}")

    def _on_release(self, _event) -> None:
        self._save_pos()

    def _pos_key(self) -> str:
        return f"{self.CODE}_pos"

    def _apply_saved_pos(self) -> None:
        self.update_idletasks()  # 同步几何信息（winfo_* 否则可能读到 0）
        sec = self._cfg.get("widgets")
        pos = sec.get(self._pos_key()) if isinstance(sec, dict) else None
        if isinstance(pos, list) and len(pos) == 2:
            try:
                self.geometry(f"+{int(pos[0])}+{int(pos[1])}")
                return
            except (ValueError, tk.TclError):
                pass
        # 默认：主屏右上角区域，burn 在上 eta 在下（避免首见重叠）
        sw = self.winfo_screenwidth()
        if sw <= 0:
            sw = 1920
        sh = self.winfo_screenheight() or 1080
        default_y = 80 if self.CODE == "burn" else 250
        self.geometry(f"+{max(sw - self.WIDTH - 40, 0)}+{min(default_y, max(sh - 200, 80))}")

    def _save_pos(self) -> None:
        try:
            x, y = self.winfo_x(), self.winfo_y()
            sec = self._cfg.setdefault("widgets", {})
            if not isinstance(sec, dict):
                return
            sec[self._pos_key()] = [x, y]
            save_config(self._cfg, config_path())
        except Exception as exc:  # noqa: BLE001 —— 位置保存失败不打扰
            _dbg(f"{self.CODE} save_pos failed: {exc!r}")
