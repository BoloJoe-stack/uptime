"""panel · uptime 可视化主面板（tkinter 深色 dashboard，纯鼠标操作）。

对外形象＝服务监控面板：深色底、卡片网格、状态点、克制绿色高亮；
窗口标题 "uptime - panel"，文案全部中性（监控/dashboard 风）。

结构：顶栏（标题+副标+运行中计数）→ 三模块卡片网格（burn/eta/tail；burn/eta=进程内
挂件，tail=子进程终端。点卡=未运行启动 / 已运行前置，运行中高亮+结束按钮，状态每秒刷新）→
收起/恢复按钮（一键最小化面板+全部 "uptime - " 模块窗口）→
设置区（月薪/上下班时间/午休/每周工作日/托盘状态标记，可折叠，失焦即时校验，
保存写当前生效 config.json，其余键不动）→ 底栏（保存设置 / 退出）+ 状态反馈条。

复用：模块启动/前置/存活判断全部走 uptime.console 的 dispatch/_is_running/
_list_module_windows（与托盘菜单同一动作路径）；窗口前置复用 console 的
_force_foreground（AttachThreadInput 手法同 focus 模块）。

主题：颜色集中 THEME 字典（code dark + run green），字体只写名称+回退链
（等宽 JetBrains Mono→Cascadia Mono→Consolas；界面 IBM Plex Sans→
Microsoft YaHei UI→Segoe UI），间距取 8 的倍数，卡片图形 Pillow 程序化
绘制（几何/波形，缓存 %TEMP%，无二进制资源入库）。

测试钩子（QC 无头驱动）：
- UPTIME_PANEL_DUMP=1  不进 mainloop，构建全部控件后把结构清单
  （卡片清单+状态绑定、按钮清单+绑定、设置字段清单、窗口标题）打印到
  stdout 退出，rc=0
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from typing import Any, Callable

import pywintypes
import win32con
import win32gui
import win32process

from uptime import console as _default_console
from uptime.common import auto_exit_seconds, load_config

try:
    from PIL import Image, ImageDraw
except ImportError:  # 与 console 同款容忍：缺失时 main 给出可读提示
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# 常量与主题（颜色集中定义，控件一律引用 THEME，禁止散落硬编码色值）
# ---------------------------------------------------------------------------
WINDOW_TITLE = "uptime - panel"
MODULE_PREFIX = "uptime - "          # 全部模块窗口标题前缀（含面板自身）

# 卡片清单：(代号, 中性中文描述)；focus 不做卡片；burn/eta=进程内挂件，tail=子进程终端
PANEL_CARDS: tuple[tuple[str, str], ...] = (
    ("burn", "成本速率监控"),
    ("eta", "交付倒计时"),
    ("tail", "构建日志流"),
)

WIDGET_CODES = ("burn", "eta")  # 进程内挂件代号（与 console 侧一致）


def _widget_class(code: str):
    """挂件类延迟导入（避免面板启动即拉起挂件模块）。"""
    if code == "burn":
        from uptime.widget.burn import BurnWidget

        return BurnWidget
    from uptime.widget.eta import EtaWidget

    return EtaWidget

THEME: dict[str, str] = {
    "bg": "#0F172A",          # 背景
    "card": "#1B2336",        # 卡片
    "card_hover": "#1E293B",  # 卡片悬浮
    "subpanel": "#334155",    # 次级面板
    "mute": "#272F42",        # 静默块
    "fg": "#F8FAFC",          # 前景
    "fg2": "#94A3B8",         # 次级文字
    "border": "#475569",      # 边框
    "accent": "#22C55E",      # 强调绿（运行态/状态点/主按钮）
    "danger": "#EF4444",      # 危险红（结束按钮）
}

# 字体名称回退链（只写名称，不带字体文件；运行时探测系统已装家族）
MONO_CHAIN: tuple[str, ...] = ("JetBrains Mono", "Cascadia Mono", "Consolas", "Courier New")
UI_CHAIN: tuple[str, ...] = ("IBM Plex Sans", "Microsoft YaHei UI", "Segoe UI", "Tahoma")

DAY_NAMES: tuple[str, ...] = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 设置区文本字段：(config 键, 界面标签)
FIELDS: tuple[tuple[str, str], ...] = (
    ("monthly_salary", "月薪（元）"),
    ("work_start", "上班时间"),
    ("work_end", "下班时间"),
    ("lunch_break_minutes", "午休（分钟）"),
)

# 间距：全部 8 的倍数
PAD_S = 8
PAD_M = 16
PAD_L = 24

TEMP_DIR = Path(tempfile.gettempdir())

DBG_LOG = TEMP_DIR / "uptime_panel_dbg.log"


def _dbg(msg: str) -> None:
    """临时诊断日志（排查打包后点卡无响应，定位后移除）。"""
    try:
        with open(DBG_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:  # noqa: BLE001
        pass
ICON_DIR = TEMP_DIR / "uptime_panel_icons"

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

_user32 = ctypes.windll.user32

_swallow = (pywintypes.error, tk.TclError)

# ---------------------------------------------------------------------------
# 纯函数：输入校验与配置写盘（可独立测试）
# ---------------------------------------------------------------------------
def parse_salary(text: Any) -> float:
    """月薪：不小于 0 的数字；非法抛中文 ValueError。"""
    raw = str(text).strip() if text is not None else ""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError("月薪必须是不小于 0 的数字") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("月薪必须是有限数字")
    if value < 0:
        raise ValueError("月薪必须是不小于 0 的数字")
    return value


def parse_hhmm(text: Any) -> str:
    """时间：严格 HH:MM 24 小时制（09:00 合法，9:00 非法）；返回原串。"""
    raw = str(text).strip() if text is not None else ""
    if not _HHMM_RE.match(raw):
        raise ValueError("时间必须是 HH:MM 24 小时制（如 09:00）")
    return raw


def parse_nonneg_int(text: Any, label: str = "午休分钟") -> int:
    """非负整数；非法抛中文 ValueError。"""
    raw = str(text).strip() if text is not None else ""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{label}必须是不小于 0 的整数") from None
    if value < 0:
        raise ValueError(f"{label}必须是不小于 0 的整数")
    return value


def parse_workdays(selected: list[int]) -> list[int]:
    """每周工作日：0-6（0=周一）去重排序；一个都没有则报错。"""
    days = sorted({d for d in selected if isinstance(d, int) and 0 <= d <= 6})
    if not days:
        raise ValueError("每周工作日至少勾选一天")
    return days


def validate_field(name: str, text: Any) -> Any:
    """按字段名校验单值（失焦即时校验 / 保存前全量校验共用）。"""
    if name == "monthly_salary":
        return parse_salary(text)
    if name in ("work_start", "work_end"):
        return parse_hhmm(text)
    if name == "lunch_break_minutes":
        return parse_nonneg_int(text, "午休分钟")
    raise ValueError(f"未知字段：{name}")


def effective_config_path() -> Path:
    """当前生效的配置文件路径（frozen=exe 旁；源码=仓库根；与 common 同口径）。"""
    from uptime.common.config import _default_config_path

    return _default_config_path()


def save_settings(
    *,
    salary_text: str,
    start_text: str,
    end_text: str,
    lunch_text: str,
    workdays: list[int],
    show_state: bool,
    path: str | Path | None = None,
) -> Path:
    """校验并写盘设置：只动 monthly_salary/work_start/work_end/
    lunch_break_minutes/workdays/console.show_state，其余键原样保留。

    任一字段非法抛中文 ValueError（不写盘）；写盘失败抛 OSError。
    """
    salary = parse_salary(salary_text)
    start = parse_hhmm(start_text)
    end = parse_hhmm(end_text)
    lunch = parse_nonneg_int(lunch_text, "午休分钟")
    days = parse_workdays(workdays)

    cfg = load_config(path)
    console_sec = cfg.get("console")
    if not isinstance(console_sec, dict):
        console_sec = {}
    cfg["monthly_salary"] = salary
    cfg["work_start"] = start
    cfg["work_end"] = end
    cfg["lunch_break_minutes"] = lunch
    cfg["workdays"] = days
    console_sec["show_state"] = bool(show_state)
    cfg["console"] = console_sec

    target = Path(path) if path is not None else effective_config_path()
    target.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def settings_defaults(path: str | Path | None = None) -> dict[str, Any]:
    """设置区初始值：读当前生效配置（缺键取保守默认）。"""
    cfg = load_config(path)
    console_sec = cfg.get("console") or {}
    if not isinstance(console_sec, dict):
        console_sec = {}
    salary = cfg.get("monthly_salary", 0)
    salary_text = str(int(salary)) if float(salary).is_integer() else str(salary)
    return {
        "monthly_salary": salary_text,
        "work_start": str(cfg.get("work_start", "09:00")),
        "work_end": str(cfg.get("work_end", "18:00")),
        "lunch_break_minutes": str(cfg.get("lunch_break_minutes", 60)),
        "workdays": list(cfg.get("workdays", [0, 1, 2, 3, 4])),
        "show_state": bool(console_sec.get("show_state", False)),
    }


# ---------------------------------------------------------------------------
# 卡片图形：Pillow 程序化绘制（几何/波形，缓存 %TEMP%，无二进制资源入库）
# ---------------------------------------------------------------------------
_ICON_PX = 44
_ICON_SS = 4  # 4 倍超采样抗锯齿


def _rgb(hexstr: str) -> tuple[int, int, int, int]:
    return (int(hexstr[1:3], 16), int(hexstr[3:5], 16), int(hexstr[5:7], 16), 255)


def _hex_to_rgb(hexstr: str) -> tuple[int, int, int, int]:
    """THEME 色值 -> PIL RGBA（函数名更直白，供绘制使用）。"""
    return _rgb(hexstr)


def _draw_card_icon(code: str):
    """按代号画几何图形（44x44 显示尺寸，176x176 超采样后缩回）。"""
    size = _ICON_PX * _ICON_SS
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    accent = _hex_to_rgb(THEME["accent"])
    dim = _hex_to_rgb(THEME["fg2"])
    bold = 3 * _ICON_SS
    if code == "burn":  # 燃尽图：下行阶梯折线 + 基线
        pts = [(24, 40), (76, 40), (76, 92), (128, 92), (128, 140), (160, 140)]
        d.line(pts, fill=accent, width=bold, joint="curve")
        for p in pts:
            d.ellipse([p[0] - 6, p[1] - 6, p[0] + 6, p[1] + 6], fill=accent)
        d.line([(24, 160), (160, 160)], fill=dim, width=bold)
    elif code == "eta":  # 时钟：圆环 + 指针
        d.ellipse([24, 24, 152, 152], outline=accent, width=bold)
        d.line([(88, 88), (88, 44)], fill=accent, width=bold)
        d.line([(88, 88), (122, 104)], fill=accent, width=bold)
        d.ellipse([80, 80, 96, 96], fill=accent)
    elif code == "tail":  # 日志流：长短交错的行 + 行首方块
        rows = [(40, 120), (64, 84), (88, 132), (112, 72), (136, 100)]
        for y, length in rows:
            d.rectangle([24, y - 5, 40, y + 5], fill=dim)
            d.line([(52, y), (52 + length, y)], fill=accent, width=bold)
    return img.resize((_ICON_PX, _ICON_PX), Image.LANCZOS)


def _draw_window_icon():
    """面板窗口图标：深色圆角底 + 绿色波形 + 状态点（与托盘图标同观感）。"""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, 62, 62], radius=13, fill=_hex_to_rgb(THEME["bg"]))
    wave = [(10, 38), (18, 38), (23, 25), (29, 49), (34, 38), (44, 38), (50, 30), (54, 38)]
    d.line(wave, fill=_hex_to_rgb(THEME["accent"]), width=3, joint="curve")
    d.ellipse([49, 11, 57, 19], fill=_hex_to_rgb(THEME["accent"]))
    return img


def ensure_icon_pngs() -> dict[str, Path]:
    """生成/复用全部图形 PNG（缓存 %TEMP%/uptime_panel_icons）。

    返回 {代号: 路径}，外加 "__window__" -> 面板窗口图标路径。
    """
    if Image is None or ImageDraw is None:
        raise RuntimeError("缺少 Pillow（py -3.10 -m pip install Pillow）")
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    for code, _desc in PANEL_CARDS:
        p = ICON_DIR / f"uptime_panel_{code}.png"
        if not p.is_file():
            _draw_card_icon(code).save(p, "PNG")
        out[code] = p
    pwin = ICON_DIR / "uptime_panel_window.png"
    if not pwin.is_file():
        _draw_window_icon().save(pwin, "PNG")
    out["__window__"] = pwin
    return out

# ---------------------------------------------------------------------------
# 窗口枚举/最小化/还原（pywin32；等价重写 focus 模块手法，不依赖键盘钩子）
# ---------------------------------------------------------------------------
def list_windows_by_prefix(prefix: str) -> list[tuple[int, str]]:
    """可见顶层窗口中标题以 prefix 开头的 [(hwnd, title)]。

    用 strip+前缀容错（终端宿主可能在标题后追加后缀）；枚举瞬间窗口销毁不中断。
    """
    out: list[tuple[int, str]] = []

    def _cb(hwnd: int, _extra: Any) -> bool:
        try:
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if title.strip().startswith(prefix):
                    out.append((hwnd, title))
        except pywintypes.error:
            pass
        return True

    win32gui.EnumWindows(_cb, None)
    return out


def _pid_of(hwnd: int) -> int:
    """窗口所属进程 pid（查询失败返回 -1）。"""
    try:
        _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        return int(pid)
    except pywintypes.error:
        return -1


def _find_panel_hwnd() -> int:
    """本进程的 "uptime - panel" 顶层窗口句柄（找不到返回 0）。"""
    for hwnd, _title in list_windows_by_prefix(WINDOW_TITLE):
        if _pid_of(hwnd) == os.getpid():
            return hwnd
    return 0


def minimize_hwnds(hwnds: list[int]) -> list[dict[str, Any]]:
    """最小化一组窗口（ShowWindowAsync 异步派发），记录原显示状态供还原。"""
    saved: list[dict[str, Any]] = []
    for hwnd in hwnds:
        try:
            if not win32gui.IsWindow(hwnd) or win32gui.IsIconic(hwnd):
                continue
            show_cmd = win32gui.GetWindowPlacement(hwnd)[1]
            _user32.ShowWindowAsync(hwnd, win32con.SW_MINIMIZE)
            saved.append({"hwnd": hwnd, "show_cmd": int(show_cmd)})
        except _swallow:
            continue
    return saved


def restore_hwnds(saved: list[dict[str, Any]]) -> int:
    """按记录还原窗口（SW_MAXIMIZE/SW_RESTORE 视原状态）；返回还原数。"""
    count = 0
    for entry in saved:
        hwnd = int(entry.get("hwnd", 0))
        try:
            if hwnd and win32gui.IsWindow(hwnd) and win32gui.IsIconic(hwnd):
                prev = entry.get("show_cmd", win32con.SW_SHOWNORMAL)
                cmd = (
                    win32con.SW_MAXIMIZE
                    if prev == win32con.SW_SHOWMAXIMIZED
                    else win32con.SW_RESTORE
                )
                _user32.ShowWindowAsync(hwnd, cmd)
                count += 1
        except _swallow:
            continue
    return count


# ---------------------------------------------------------------------------
# 结束模块：干净结束进程树 + 窗口
# ---------------------------------------------------------------------------
def stop_module(console_mod: Any = None, code: str = "") -> bool:
    """结束一个运行中的模块：先 WM_CLOSE 礼貌关窗，超时 taskkill /T /F 清进程树。

    支持两类对象：本壳启动的子进程（console._procs）与外部模块窗口。
    返回 True=已结束（进程退出且无残留窗口）。
    """
    mod = console_mod or _default_console
    with mod._state_lock:
        proc = mod._procs.get(code)

    # 1) 礼貌路径：对模块控制台窗口发 WM_CLOSE（等价点窗口的 X）
    for hwnd, _title in mod._list_module_windows(code):
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except pywintypes.error:
            pass
    deadline = time.time() + 2.0
    while time.time() < deadline:
        gone = (proc is None or proc.poll() is not None) and not mod._list_module_windows(code)
        if gone:
            break
        time.sleep(0.1)

    # 2) 强制路径：进程树还在则 taskkill /T /F（连控制台窗口一起收掉）
    if proc is not None and proc.poll() is None:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except OSError:
            pass
    for hwnd, _title in mod._list_module_windows(code):
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except pywintypes.error:
            pass

    # 3) 收尾：从子进程表移除并回收（句柄干净）
    with mod._state_lock:
        fin = mod._procs.pop(code, None)
    if fin is not None:
        try:
            fin.wait(timeout=3)
        except Exception:  # noqa: BLE001 —— taskkill 后基本立即结束
            pass
    time.sleep(0.2)
    return (fin is None or fin.poll() is not None) and not mod._list_module_windows(code)

# ---------------------------------------------------------------------------
# UI 类：深色主题主窗口
# ---------------------------------------------------------------------------
def _pick_family(root: tk.Tk, chain: tuple[str, ...]) -> str:
    """按回退链选系统已装字体家族；探测失败用链尾兜底。"""
    try:
        families = set(root.tk.call("font", "families"))
    except _swallow:
        return chain[-1]
    for name in chain:
        if name in families:
            return name
    return chain[-1]


class PanelApp:
    """uptime 主面板。

    hosted（由 console 壳托管的常驻形态）与 standalone（直接 -m uptime.panel）
    两种运行方式共用；区别只在退出动作与 X 关闭语义：
    hosted + close_to_tray → 隐藏到托盘进程不死；否则退出。
    """

    def __init__(
        self,
        cfg: dict[str, Any] | None = None,
        *,
        icon: Any = None,
        on_shell_exit: Callable[[], None] | None = None,
        console_mod: Any = None,
    ) -> None:
        if Image is None or ImageDraw is None:
            raise RuntimeError("缺少 Pillow（py -3.10 -m pip install Pillow）")
        self.cfg = cfg or {}
        self.tray_icon = icon
        self.on_shell_exit = on_shell_exit
        self._hosted = on_shell_exit is not None
        self.close_to_tray = bool(self.cfg.get("close_to_tray", True))
        self._console = console_mod or _default_console

        self._running: dict[str, bool] = {}
        self._hover: dict[str, bool] = {}
        self._focus: dict[str, bool] = {}
        self._hidden: list[dict[str, Any]] = []
        self._widgets: dict[str, Any] = {}  # burn/eta 进程内挂件
        self._default_geometry = "784x640"

        self._icons = ensure_icon_pngs()
        self._build()

    # -- 构建窗口骨架 -------------------------------------------------------
    def _build(self) -> None:
        self.root = tk.Tk()
        self.root.report_callback_exception = self._dbg_callback_exc
        self.root.title(WINDOW_TITLE)
        self.root.configure(bg=THEME["bg"])
        self.root.geometry(self._default_geometry)
        self.root.minsize(784, 560)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        mono = _pick_family(self.root, MONO_CHAIN)
        ui = _pick_family(self.root, UI_CHAIN)
        self.fonts = {
            "title": (ui, 15, "bold"),
            "sub": (ui, 9),
            "section": (ui, 10, "bold"),
            "body": (ui, 9),
            "body_bold": (ui, 9, "bold"),
            "btn": (ui, 9, "bold"),
            "btn_main": (ui, 10, "bold"),
            "code": (mono, 14, "bold"),
            "dot": (mono, 14, "bold"),
            "entry": (ui, 10),
        }
        try:
            self._photo_window = tk.PhotoImage(
                master=self.root, file=str(self._icons["__window__"])
            )
            self.root.iconphoto(False, self._photo_window)
        except _swallow:
            self._photo_window = None

        # 底栏与状态条先 pack 到底部占位，其余自上而下
        self._build_bottom()
        self._build_status()
        self._build_topbar()
        self._build_cards()
        self._build_stealth()
        self._build_settings()

    def _build_topbar(self) -> None:
        bar = tk.Frame(self.root, bg=THEME["bg"])
        bar.pack(fill="x", padx=PAD_M, pady=(PAD_M, PAD_S))
        tk.Label(
            bar, text="uptime", font=self.fonts["title"], fg=THEME["fg"], bg=THEME["bg"]
        ).pack(side="left")
        tk.Label(
            bar, text="服务运行监控面板", font=self.fonts["sub"],
            fg=THEME["fg2"], bg=THEME["bg"],
        ).pack(side="left", padx=PAD_M)
        self.count_var = tk.StringVar(value=f"运行中 0/{len(PANEL_CARDS)}")
        tk.Label(
            bar, textvariable=self.count_var, font=self.fonts["body_bold"],
            fg=THEME["fg"], bg=THEME["bg"],
        ).pack(side="right")

    # -- 模块卡片网格 -------------------------------------------------------
    def _build_cards(self) -> None:
        wrap = tk.Frame(self.root, bg=THEME["bg"])
        wrap.pack(fill="both", expand=True, padx=PAD_M, pady=PAD_S)
        for col in range(3):
            wrap.grid_columnconfigure(col, weight=1, uniform="cards")
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_rowconfigure(1, weight=1)

        self._card: dict[str, tk.Frame] = {}
        self._card_bg_widgets: dict[str, list[tk.Widget]] = {}
        self._dot: dict[str, tk.Label] = {}
        self._code_label: dict[str, tk.Label] = {}
        self._stop_btn: dict[str, tk.Button] = {}
        self._photo_card: dict[str, tk.PhotoImage] = {}

        for index, (code, desc) in enumerate(PANEL_CARDS):
            self._build_card(wrap, index, code, desc)

    def _build_card(self, parent: tk.Widget, index: int, code: str, desc: str) -> None:
        row, col = divmod(index, 3)
        card = tk.Frame(
            parent, bg=THEME["card"], cursor="hand2", takefocus=1,
            highlightthickness=1, highlightbackground=THEME["border"],
        )
        card.grid(row=row, column=col, sticky="nsew", padx=PAD_S, pady=PAD_S)
        card.configure(width=232, height=160)
        card.pack_propagate(False)
        card.grid_propagate(False)

        head = tk.Frame(card, bg=THEME["card"])
        head.pack(fill="x", padx=PAD_M, pady=(PAD_M, 0))
        photo = tk.PhotoImage(master=card, file=str(self._icons[code]))
        self._photo_card[code] = photo
        icon_lbl = tk.Label(head, image=photo, bg=THEME["card"])
        icon_lbl.pack(side="left")
        dot = tk.Label(
            head, text="○", font=self.fonts["dot"], fg=THEME["fg2"], bg=THEME["card"]
        )
        dot.pack(side="right")
        code_lbl = tk.Label(
            card, text=code, font=self.fonts["code"], fg=THEME["fg"], bg=THEME["card"]
        )
        code_lbl.pack(anchor="w", padx=PAD_M)
        desc_lbl = tk.Label(
            card, text=desc, font=self.fonts["body"], fg=THEME["fg2"], bg=THEME["card"]
        )
        desc_lbl.pack(anchor="w", padx=PAD_M)

        btnrow = tk.Frame(card, bg=THEME["card"])
        btnrow.pack(fill="x", padx=PAD_M, pady=(0, PAD_M))
        stop = tk.Button(
            btnrow, text="结束", font=self.fonts["btn"], cursor="hand2",
            bg=THEME["danger"], fg=THEME["fg"], activebackground=THEME["danger"],
            activeforeground=THEME["fg"], relief="flat", bd=0, pady=6, padx=PAD_M,
            command=self._make_stop(code),
        )
        stop.pack(side="right")
        stop.pack_forget()  # 未运行时隐藏

        # 整卡可点：非按钮子控件并入卡片 bindtag，卡片绑定对整卡生效
        for widget in (card, head, icon_lbl, code_lbl, desc_lbl, dot):
            if widget is not card:
                widget.configure(cursor="hand2")
                widget.bindtags(widget.bindtags() + (card,))
            card.bind("<Button-1>", lambda _e, c=code: self._activate(c))
            card.bind("<Enter>", lambda _e, c=code: self._set_hover(c, True))
            card.bind("<Leave>", lambda _e, c=code: self._set_hover(c, False))
            card.bind("<FocusIn>", lambda _e, c=code: self._set_focus(c, True))
            card.bind("<FocusOut>", lambda _e, c=code: self._set_focus(c, False))

        self._card[code] = card
        self._card_bg_widgets[code] = [head, btnrow, icon_lbl, code_lbl, desc_lbl]
        self._dot[code] = dot
        self._code_label[code] = code_lbl
        self._stop_btn[code] = stop

    def _set_hover(self, code: str, state: bool) -> None:
        self._hover[code] = state
        self._apply_card_style(code)

    def _set_focus(self, code: str, state: bool) -> None:
        self._focus[code] = state
        self._apply_card_style(code)

    def _apply_card_style(self, code: str) -> None:
        """状态切换必须看得见：悬浮/运行/焦点 → 底色、边框、代号色、状态点即时变化。"""
        running = self._running.get(code, False)
        hover = self._hover.get(code, False)
        focused = self._focus.get(code, False)
        bg = THEME["card_hover"] if hover else THEME["card"]
        border = THEME["accent"] if (running or hover or focused) else THEME["border"]
        card = self._card[code]
        card.configure(bg=bg, highlightbackground=border)
        for widget in self._card_bg_widgets[code]:
            widget.configure(bg=bg)
        self._dot[code].configure(
            text="●" if running else "○",
            fg=THEME["accent"] if running else THEME["fg2"],
        )
        self._code_label[code].configure(
            fg=THEME["accent"] if running else THEME["fg"]
        )
        btn = self._stop_btn[code]
        if running:
            btn.pack(side="right")
        else:
            btn.pack_forget()

    # -- 收起/恢复按钮 -------------------------------------------------------
    def _build_stealth(self) -> None:
        bar = tk.Frame(self.root, bg=THEME["bg"])
        bar.pack(fill="x", padx=PAD_M, pady=PAD_S)
        self.stealth_btn = tk.Button(
            bar, text="收起全部", font=self.fonts["btn"], cursor="hand2",
            bg=THEME["mute"], fg=THEME["fg"], activebackground=THEME["subpanel"],
            activeforeground=THEME["fg"], relief="flat", bd=0, pady=6, padx=PAD_M,
            command=self._stealth_toggle,
        )
        self.stealth_btn.pack(side="left")
        tk.Label(
            bar, text="最小化面板与全部 uptime 模块窗口（面板可从任务栏找回）",
            font=self.fonts["body"], fg=THEME["fg2"], bg=THEME["bg"],
        ).pack(side="left", padx=PAD_M)

    def _stealth_toggle(self) -> None:
        """双向开关：有收起记录=恢复全部，否则=收起面板+全部模块窗口。"""
        if self._hidden:
            self._restore_all()
        else:
            self._collapse_all()

    def _collapse_all(self) -> None:
        targets: list[int] = []
        for hwnd, _title in list_windows_by_prefix(MODULE_PREFIX):
            if _pid_of(hwnd) == os.getpid():
                continue  # 面板自身与挂件走各自路径，不进还原清单
            targets.append(hwnd)
        self._hidden = minimize_hwnds(targets)
        for w in self._widgets.values():
            if w.alive():
                w.hide()
        self.root.iconify()
        self.stealth_btn.configure(text="恢复全部")
        self._status(f"已收起 {len(self._hidden)} 个窗口（面板可从任务栏找回）")

    def _restore_all(self) -> None:
        count = restore_hwnds(self._hidden)
        self._hidden = []
        for w in self._widgets.values():
            if w.alive():
                w.show()
        try:
            if self.root.state() == "iconic":
                self.root.deiconify()
            self.root.lift()
        except _swallow:
            pass
        self.stealth_btn.configure(text="收起全部")
        self._status(f"已恢复 {count} 个窗口")

    # -- 设置区（可折叠；每字段可见标签 + 失焦即时校验 + 贴字段错误提示） ----
    def _build_settings(self) -> None:
        self._settings_open = False
        head = tk.Frame(self.root, bg=THEME["bg"])
        head.pack(fill="x", padx=PAD_M, pady=(PAD_S, 0))
        tk.Label(
            head, text="设 置", font=self.fonts["section"],
            fg=THEME["fg2"], bg=THEME["bg"],
        ).pack(side="left")
        self._settings_toggle_btn = tk.Button(
            head, text="展开", font=self.fonts["btn"], cursor="hand2",
            bg=THEME["bg"], fg=THEME["fg2"], activebackground=THEME["bg"],
            activeforeground=THEME["fg"], relief="flat", bd=0,
            command=self._toggle_settings,
        )
        self._settings_toggle_btn.pack(side="left", padx=PAD_M)

        body = tk.Frame(self.root, bg=THEME["card"], highlightthickness=1,
                        highlightbackground=THEME["border"])
        self.settings_frame = body

        defaults = settings_defaults()
        self.vars: dict[str, tk.StringVar] = {}
        self.err_vars: dict[str, tk.StringVar] = {}
        self.entries: dict[str, tk.Entry] = {}
        for row, (name, label) in enumerate(FIELDS):
            tk.Label(
                body, text=label, font=self.fonts["body"], fg=THEME["fg2"],
                bg=THEME["card"], width=14, anchor="e",
            ).grid(row=row, column=0, sticky="e", padx=PAD_S, pady=PAD_S)
            var = tk.StringVar(value=str(defaults[name]))
            entry = tk.Entry(
                body, textvariable=var, font=self.fonts["entry"], width=14,
                bg=THEME["mute"], fg=THEME["fg"], insertbackground=THEME["fg"],
                relief="flat", highlightthickness=1,
                highlightbackground=THEME["border"], highlightcolor=THEME["accent"],
            )
            entry.grid(row=row, column=1, sticky="w", padx=PAD_S, pady=PAD_S)
            err = tk.StringVar(value="")
            tk.Label(
                body, textvariable=err, font=self.fonts["body"], fg=THEME["danger"],
                bg=THEME["card"], wraplength=240, justify="left",
            ).grid(row=row, column=2, sticky="w", padx=PAD_S, pady=PAD_S)
            entry.bind("<FocusOut>", lambda _e, n=name: self._on_field_focusout(n))
            self.vars[name] = var
            self.err_vars[name] = err
            self.entries[name] = entry

        # 每周工作日（勾选周一~周日）
        row = len(FIELDS)
        tk.Label(
            body, text="每周工作日", font=self.fonts["body"], fg=THEME["fg2"],
            bg=THEME["card"], width=14, anchor="e",
        ).grid(row=row, column=0, sticky="e", padx=PAD_S, pady=PAD_S)
        days_wrap = tk.Frame(body, bg=THEME["card"])
        days_wrap.grid(row=row, column=1, columnspan=2, sticky="w", padx=PAD_S, pady=PAD_S)
        self.day_vars = []
        for idx, day in enumerate(DAY_NAMES):
            dv = tk.BooleanVar(value=idx in defaults["workdays"])
            tk.Checkbutton(
                days_wrap, text=day, variable=dv, font=self.fonts["body"],
                fg=THEME["fg"], bg=THEME["card"], activebackground=THEME["card"],
                activeforeground=THEME["fg"], selectcolor=THEME["mute"],
                cursor="hand2", relief="flat", bd=0, highlightthickness=0,
                padx=PAD_S,
            ).pack(side="left")
            self.day_vars.append(dv)
        self.err_vars["workdays"] = tk.StringVar(value="")
        self.days_wrap = days_wrap

        # 托盘状态标记开关（console.show_state）
        row += 1
        tk.Label(
            body, text="托盘状态标记", font=self.fonts["body"], fg=THEME["fg2"],
            bg=THEME["card"], width=14, anchor="e",
        ).grid(row=row, column=0, sticky="e", padx=PAD_S, pady=PAD_S)
        self.show_state_var = tk.BooleanVar(value=bool(defaults["show_state"]))
        tk.Checkbutton(
            body, text="托盘菜单显示运行状态 (●/○)", variable=self.show_state_var,
            font=self.fonts["body"], fg=THEME["fg"], bg=THEME["card"],
            activebackground=THEME["card"], activeforeground=THEME["fg"],
            selectcolor=THEME["mute"], cursor="hand2", relief="flat", bd=0,
            highlightthickness=0,
        ).grid(row=row, column=1, columnspan=2, sticky="w", padx=PAD_S, pady=PAD_S)

        body.pack(fill="x", padx=0, pady=(PAD_S, PAD_S))
        body.pack_forget()  # 默认折叠

    def _toggle_settings(self) -> None:
        if self._settings_open:
            self.settings_frame.pack_forget()
            self._settings_toggle_btn.configure(text="展开")
            self.root.geometry(self._default_geometry)
        else:
            self.settings_frame.pack(fill="x", pady=(PAD_S, PAD_S))
            self._settings_toggle_btn.configure(text="收起")
            try:
                need = self.root.winfo_reqheight() + PAD_S
                if need > self.root.winfo_height():
                    self.root.geometry(f"{self.root.winfo_width()}x{need}")
            except _swallow:
                pass
        self._settings_open = not self._settings_open

    def _on_field_focusout(self, name: str) -> None:
        """失焦即时校验：错误提示贴在字段旁（不只底部汇总）。"""
        try:
            validate_field(name, self.vars[name].get())
            self._field_ok(name)
        except ValueError as exc:
            self._field_error(name, str(exc))

    def _field_error(self, name: str, msg: str) -> None:
        self.err_vars[name].set(msg)
        try:
            self.entries[name].configure(highlightbackground=THEME["danger"])
        except KeyError:
            pass

    def _field_ok(self, name: str) -> None:
        self.err_vars[name].set("")
        try:
            self.entries[name].configure(highlightbackground=THEME["border"])
        except KeyError:
            pass

    # -- 状态反馈条 + 底栏（保存设置 / 退出） --------------------------------
    def _build_status(self) -> None:
        self.status_var = tk.StringVar(value="就绪")
        self.status_label = tk.Label(
            self.root, textvariable=self.status_var, font=self.fonts["body"],
            fg=THEME["fg2"], bg=THEME["mute"], anchor="w", padx=PAD_M,
        )
        self.status_label.pack(fill="x", side="bottom", padx=PAD_M, pady=(PAD_S, PAD_S))

    def _build_bottom(self) -> None:
        bar = tk.Frame(self.root, bg=THEME["bg"])
        bar.pack(fill="x", side="bottom", padx=PAD_M, pady=(PAD_S, PAD_M))
        save = tk.Button(
            bar, text="保存设置", font=self.fonts["btn_main"], cursor="hand2",
            bg=THEME["accent"], fg=THEME["bg"], activebackground=THEME["accent"],
            activeforeground=THEME["bg"], relief="flat", bd=0, pady=10,
            padx=PAD_L, command=self._save,
        )
        save.pack(side="left")
        exit_btn = tk.Button(
            bar, text="退出", font=self.fonts["btn_main"], cursor="hand2",
            bg=THEME["danger"], fg=THEME["fg"], activebackground=THEME["danger"],
            activeforeground=THEME["fg"], relief="flat", bd=0, pady=10,
            padx=PAD_L, command=self._exit_app,
        )
        exit_btn.pack(side="right")

    def _status(self, text: str, kind: str = "info") -> None:
        """状态反馈条：一切状态变化有文字反馈，不静默。"""
        color = {"info": THEME["fg2"], "ok": THEME["accent"], "error": THEME["danger"]}
        try:
            self.status_var.set(text)
            self.status_label.configure(fg=color.get(kind, THEME["fg2"]))
        except _swallow:
            pass

    # -- 卡片动作：启动/前置、结束 ------------------------------------------
    def _dbg_callback_exc(self, exc, val, tb) -> None:  # 临时诊断
        import traceback
        _dbg("tk callback exception: " + "".join(traceback.format_exception(exc, val, tb)))

    def _dbg_dump_cards(self) -> None:  # 临时诊断：卡片屏幕坐标（供外部模拟点击）
        try:
            for code, card in self._card.items():
                _dbg(
                    f"card_rect {code} {card.winfo_rootx()},{card.winfo_rooty()} "
                    f"{card.winfo_width()}x{card.winfo_height()}"
                )
        except _swallow:
            pass

    # -- 进程内挂件（burn/eta）：与面板同进程的 Toplevel ----------------------
    def toggle_widget(self, code: str) -> str:
        """未开→新建挂件；已开→前置。返回 "started"/"foreground"（与 dispatch 同口径）。"""
        w = self._widgets.get(code)
        if w is not None and w.alive():
            w.show()
            _dbg(f"toggle_widget {code} -> foreground")
            return "foreground"
        cls = _widget_class(code)
        self._widgets[code] = cls(self.root, self.cfg, on_close=self._on_widget_closed)
        _dbg(f"toggle_widget {code} -> started")
        return "started"

    def _on_widget_closed(self, code: str) -> None:
        self._widgets.pop(code, None)
        self.refresh()

    def _activate(self, code: str) -> None:
        """点卡：burn/eta=进程内挂件开/前置；tail=子进程新控制台（console.dispatch）。"""
        _dbg(f"activate {code}")
        try:
            if code in WIDGET_CODES:
                result = self.toggle_widget(code)
            else:
                result = self._console.dispatch(code)
        except Exception as exc:  # noqa: BLE001 —— UI 回调兜底
            _dbg(f"activate {code} exception: {exc!r}")
            self._status(f"{code} 启动失败：{exc}", "error")
            return
        _dbg(f"activate {code} dispatch -> {result}")
        if result == "started":
            self._status(f"{code} 已启动")
        elif result == "foreground":
            self._status(f"{code} 已前置")
        else:
            self._status(f"{code} 前置失败", "error")
        self.refresh()

    def _make_stop(self, code: str) -> Callable[[], None]:
        def _stop() -> None:
            w = self._widgets.get(code)
            if w is not None and w.alive():
                w.close()  # on_close 回调里会 refresh
                self._status(f"{code} 已结束")
                return
            ok = stop_module(self._console, code)
            if ok:
                self._status(f"{code} 已结束")
            else:
                self._status(f"{code} 结束异常（请检查残留窗口）", "error")
            self.refresh()

        return _stop

    # -- 设置保存 ------------------------------------------------------------
    def _save(self) -> None:
        ok_all = True
        for name, _label in FIELDS:
            try:
                validate_field(name, self.vars[name].get())
                self._field_ok(name)
            except ValueError as exc:
                self._field_error(name, str(exc))
                ok_all = False
        days = [idx for idx, dv in enumerate(self.day_vars) if dv.get()]
        try:
            parse_workdays(days)
            self.err_vars["workdays"].set("")
        except ValueError as exc:
            self.err_vars["workdays"].set(str(exc))
            ok_all = False
        if not ok_all:
            self._status("存在无效设置（红字标注），未写盘", "error")
            return
        try:
            save_settings(
                salary_text=self.vars["monthly_salary"].get(),
                start_text=self.vars["work_start"].get(),
                end_text=self.vars["work_end"].get(),
                lunch_text=self.vars["lunch_break_minutes"].get(),
                workdays=days,
                show_state=self.show_state_var.get(),
            )
        except ValueError as exc:
            self._status(f"保存失败：{exc}", "error")
            return
        except OSError as exc:
            self._status(f"保存失败：无法写配置文件（{exc}）", "error")
            return
        self._status("已保存，模块下次启动生效", "ok")

    # -- 状态每秒刷新 --------------------------------------------------------
    def _is_running(self, code: str) -> bool:
        """运行中=进程内挂件存活，或本壳子进程存活，或桌面已有该模块窗口。"""
        w = self._widgets.get(code)
        if w is not None and w.alive():
            return True
        return self._console._is_running(code) or bool(
            self._console._list_module_windows(code)
        )

    def refresh(self) -> None:
        running = 0
        for code, _desc in PANEL_CARDS:
            alive = self._is_running(code)
            self._running[code] = alive
            if alive:
                running += 1
            self._apply_card_style(code)
        self.count_var.set(f"运行中 {running}/{len(PANEL_CARDS)}")

    def _tick(self) -> None:
        try:
            self.refresh()
            if not getattr(self, "_dbg_dumped", False):  # 启动时记一次卡片坐标
                self._dbg_dumped = True
                self._dbg_dump_cards()
        except _swallow:
            return
        try:
            # 二次双击唤起：第二实例写标记文件，这里消费并弹出面板
            flag = getattr(self._console, "SHOW_PANEL_FLAG", None)
            if flag is not None and flag.is_file():
                try:
                    flag.unlink()
                except OSError:
                    pass
                self.show()
        except _swallow:
            pass
        try:
            self.root.after(1000, self._tick)
        except _swallow:
            pass

    # -- 显示 / 隐藏 / 关闭 / 退出 ------------------------------------------
    def show(self) -> None:
        """显示/前置面板（托盘 panel 项路径；任意线程可调，转发到 tk 线程）。"""
        try:
            self.root.after(0, self._show_now)
        except (RuntimeError, tk.TclError):
            pass

    def _show_now(self) -> None:
        _dbg("show_now enter")  # 临时诊断
        try:
            if not self.root.winfo_exists():
                return
            if self.root.state() in ("withdrawn", "iconic"):
                self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            hwnd = _find_panel_hwnd()
            _dbg(f"show_now hwnd={hwnd}")  # 临时诊断
            if hwnd:
                # AttachThreadInput 系调用可能阻塞：挪到后台线程，防卡死 tk 主循环
                threading.Thread(
                    target=self._console._force_foreground, args=(hwnd,), daemon=True
                ).start()
        except _swallow:
            _dbg(f"show_now exc")  # 临时诊断
        finally:
            _dbg("show_now exit")  # 临时诊断

    def _on_close(self) -> None:
        """X 关闭：hosted+close_to_tray → 隐藏到托盘（进程不死）；否则整壳退出。"""
        if self._hosted and self.close_to_tray:
            self.root.withdraw()
            self._status("已隐藏到托盘（托盘菜单 panel 可找回）")
        else:
            self._exit_app()

    def _exit_app(self) -> None:
        if self.on_shell_exit is not None:
            self._status("正在退出…")
            self.on_shell_exit()
        else:
            self.request_quit()

    def request_quit(self) -> None:
        """线程安全请求退出 mainloop（AUTO_EXIT / 托盘 exit / 面板退出共用）。"""
        try:
            self.root.after(0, self._quit)
        except (RuntimeError, tk.TclError):
            self._quit()

    def _quit(self) -> None:
        try:
            if self.root.winfo_exists():
                self.root.destroy()
        except _swallow:
            pass

    def start_hidden(self) -> None:
        """panel_on_start=false：只进托盘，不显示面板窗口。"""
        try:
            self.root.withdraw()
        except _swallow:
            pass

    def run(self) -> None:
        self.refresh()
        self.root.after(1000, self._tick)
        # 临时自测钩子：UPTIME_PANEL_AUTOTEST="code@delay_sec" 在 tk 线程直调 _activate
        at = os.environ.get("UPTIME_PANEL_AUTOTEST")
        if at and "@" in at:
            at_code, _, at_secs = at.partition("@")
            if at_code in self._card:
                _dbg(f"autotest armed {at_code} @{at_secs}s")
                self.root.after(int(float(at_secs) * 1000),
                                lambda: self._activate(at_code))
        self.root.mainloop()

    # -- 测试钩子：结构清单 --------------------------------------------------
    def dump(self) -> None:
        lines = [
            "panel: dump",
            f"window_title: {WINDOW_TITLE}",
            f"cards: {len(PANEL_CARDS)}",
        ]
        for code, desc in PANEL_CARDS:
            alive = self._is_running(code)
            lines.append(
                f"card: {code} desc={desc} state={'●' if alive else '○'} "
                f"click=dispatch/{code} stop=stop_module/{code}"
            )
        lines.append("buttons: 5")
        lines.append("button: stealth=collapse_restore_all")
        lines.append("button: settings_toggle=expand_collapse")
        lines.append("button: save=save_settings")
        lines.append("button: exit=quit_shell")
        lines.append(f"button: stop=stop_module (per running card, x{len(PANEL_CARDS)})")
        lines.append(f"settings_fields: {len(FIELDS) + 2}")
        for name, label in FIELDS:
            lines.append(f"field: {name} label={label} validate=focusout")
        lines.append("field: workdays label=每周工作日 type=checkbox_monday_to_sunday")
        lines.append("field: show_state label=托盘状态标记 type=checkbox")
        lines.append(f"icon_dir: {ICON_DIR}")
        print("\n".join(lines), flush=True)
        self.root.destroy()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def _dump_mode() -> None:
    """UPTIME_PANEL_DUMP=1：构建全部控件 → 打印结构清单 → 退出（不进 mainloop）。"""
    try:
        cfg = _default_console._console_config()
    except ValueError as exc:
        print(f"panel: 配置错误：{exc}")
        raise SystemExit(1) from None
    app = PanelApp(cfg)
    app.dump()


def main() -> None:
    dump = os.environ.get("UPTIME_PANEL_DUMP")
    if dump is not None:
        _dump_mode()
        return

    # standalone 运行（不经 console 壳；正常使用由壳拉起）
    try:
        cfg = _default_console._console_config()
    except ValueError as exc:
        print(f"panel: 配置错误：{exc}")
        raise SystemExit(1) from None
    app = PanelApp(cfg)
    secs = auto_exit_seconds()
    if secs is not None:
        print(f"panel: auto exit {secs}s", flush=True)
        threading.Timer(secs, app.request_quit).start()
    app.run()
