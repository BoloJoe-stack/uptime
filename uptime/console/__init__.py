"""console · uptime service 托盘 + 主面板调度壳（面板 mainloop + 托盘常驻）。

对外形象＝后台监控服务：托盘 tooltip "uptime service"，菜单只出现模块代号；
可视化主面板窗口 "uptime - panel"（tkinter 深色 dashboard，纯鼠标操作）。

进程模型：主线程跑 tkinter mainloop（panel），pystray 托盘走 run_detached
独立线程；面板与托盘菜单共用同一 dispatch 动作路径与子进程表。

行为：
- 非测试模式启动：FreeConsole 消灭自身控制台黑窗（GetConsoleWindow 存在才
  Free；拿不到控制台的运行形态跳过）；panel_on_start=true（默认）面板直接
  显示，false 只进托盘。
- 面板 X 关闭：close_to_tray=true（默认）隐藏窗口到托盘、进程不死；
  false 则 X=整壳干净退出。托盘 "panel" 项=显示/前置面板。
- 托盘菜单：四模块项（burn/eta/tail/boids）+ panel 项 + exit。
  点击未运行模块 → 新控制台窗口启动（Popen CREATE_NEW_CONSOLE，cwd=仓库根，
  env 继承并加 PYTHONUTF8=1；子模块异常退出码非 0 时窗口 pause 不闪退）；
  点击已运行模块（本壳启动的，按子进程存活判断；或桌面上已有该模块窗口）→
  前置其窗口（按 "uptime - <代号>" 标题枚举，pywin32，含前台锁绕过），不重复启动。
- 菜单运行中标记：config console.show_state 开启时菜单项文本带 ●/○ 标记，
  后台线程每秒 update_menu 刷新，保证打开菜单时状态准确（默认关）。
- 全局热键：console.hotkeys 默认空对象=一个不注册（纯鼠标操作）；
  非空时按表注册（keyboard，只认已知模块代号），行为与菜单点击完全一致
  （同走 dispatch）；注册失败（键位冲突等）打印告警继续，不崩。
- exit（托盘菜单/面板退出按钮）：停托盘、注销热键、销毁面板、释放实例锁
  后退出，不动已启动的模块进程。
- 单实例保护：%TEMP% pid 文件锁（msvcrt 非阻塞锁，进程退出/崩溃自动释放），
  二次启动打印"已在运行"后以非 0 退出码退出。

测试钩子（QC 无头驱动）：
- UPTIME_AUTO_EXIT=N          N 秒后干净退出（托盘停、热键注销、面板销毁、rc=0）
- UPTIME_CONSOLE_DUMP=1       不进托盘/面板主循环，构建全部对象（图标/菜单/热键表）
                              后把菜单结构、热键映射、图标 PNG 路径打印到 stdout 退出
- UPTIME_CONSOLE_LAUNCH=<代号> 执行一次 dispatch（启动或前置）后等子进程结束再退出
"""

from __future__ import annotations

import ctypes
import msvcrt
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pywintypes
import win32con
import win32gui

from uptime.common import auto_exit_seconds, load_config, set_console_title
from uptime.common.config import PROJECT_ROOT

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
WINDOW_TITLE = "uptime - console"   # 自身控制台窗口标题（ASCII 连字符，与各模块一致）
TOOLTIP = "uptime service"          # 托盘 tooltip（对外形象）
MODULE_CODES = ("burn", "eta", "tail", "boids", "focus")  # 热键容忍全集
MENU_CODES = ("burn", "eta", "tail", "boids")             # 托盘菜单/面板卡片四模块

TEMP_DIR = Path(tempfile.gettempdir())
ICON_PATH = TEMP_DIR / "uptime_console_icon.png"
LOCK_PATH = TEMP_DIR / "uptime_console.lock"
# 二次启动唤起标记：第二实例写、首实例的面板每秒轮询消费（弹出面板）
SHOW_PANEL_FLAG = TEMP_DIR / "uptime_console_show_panel"


def _dbg(msg: str) -> None:
    """临时诊断日志（排查打包后点卡无响应，与 panel 共用一个文件，定位后移除）。"""
    try:
        import time as _t

        with open(TEMP_DIR / "uptime_panel_dbg.log", "a", encoding="utf-8") as f:
            f.write(f"{_t.strftime('%H:%M:%S')} {msg}\n")
    except Exception:  # noqa: BLE001
        pass

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

VK_MENU = 0x12  # Alt：前台锁绕过（模拟按下/抬起，同 focus 模块手法）

# 依赖：pystray + Pillow（requirements 已启用；缺失时给出可读提示）
try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:  # noqa: SIM105 —— 模块级 try/except，main 按模式给出提示
    pystray = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def _console_config(path: str | Path | None = None) -> dict[str, Any]:
    """读取 console 配置段。

    缺段/缺键全取内置默认；hotkeys 缺失或空对象=一个热键都不注册（纯鼠标默认），
    非空时缺某模块或某键值非法则跳过该模块（容忍）；类型非法抛中文 ValueError。
    """
    section = load_config(path).get("console") or {}
    if not isinstance(section, dict):
        raise ValueError(f"配置项 console 必须是 JSON 对象，当前为 {section!r}")

    raw = section.get("hotkeys")
    if raw is None:
        hotkeys = {}
    elif isinstance(raw, dict):
        hotkeys = {}
        for code in MODULE_CODES:  # 固定模块顺序，只认已知模块代号
            key = raw.get(code)
            if isinstance(key, str) and key.strip():
                hotkeys[code] = key.strip()
            # 缺该模块 / 非法类型 / 空串：容忍跳过
    else:
        raise ValueError(f"配置项 console.hotkeys 必须是 JSON 对象，当前为 {raw!r}")

    show_state = section.get("show_state", False)
    if not isinstance(show_state, bool):
        raise ValueError(f"配置项 console.show_state 必须是布尔值，当前为 {show_state!r}")
    minimize_self = section.get("minimize_self", True)
    if not isinstance(minimize_self, bool):
        raise ValueError(
            f"配置项 console.minimize_self 必须是布尔值，当前为 {minimize_self!r}"
        )
    panel_on_start = section.get("panel_on_start", True)
    if not isinstance(panel_on_start, bool):
        raise ValueError(
            f"配置项 console.panel_on_start 必须是布尔值，当前为 {panel_on_start!r}"
        )
    close_to_tray = section.get("close_to_tray", True)
    if not isinstance(close_to_tray, bool):
        raise ValueError(
            f"配置项 console.close_to_tray 必须是布尔值，当前为 {close_to_tray!r}"
        )
    return {
        "hotkeys": hotkeys,
        "show_state": show_state,
        "minimize_self": minimize_self,
        "panel_on_start": panel_on_start,
        "close_to_tray": close_to_tray,
    }


# ---------------------------------------------------------------------------
# 托盘图标：Pillow 程序化生成（深色底 + 绿色波形 + 状态圆点），缓存 %TEMP%
# ---------------------------------------------------------------------------
_ICON_SIZE = 64
_ICON_BG = (13, 17, 23, 255)      # #0d1117 深色底
_ICON_WAVE = (63, 185, 80, 255)   # #3fb950 绿色波形
_ICON_DIM = (33, 60, 42, 255)     # 暗绿（波形下方基线）
_ICON_DOT = (63, 185, 80, 255)    # 右上角状态圆点（online 指示）


def _build_icon_image():
    """画 64x64 托盘图标：圆角深色底 + 绿色服务水平折线 + 状态圆点。纯代码无资源文件。"""
    img = Image.new("RGBA", (_ICON_SIZE, _ICON_SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, 62, 62], radius=13, fill=_ICON_BG)
    # 波形：横贯中部的服务状态折线（两段平稳 + 一次波动 + 回落）
    wave = [
        (10, 38), (18, 38), (23, 25), (29, 49), (34, 38),
        (44, 38), (50, 30), (54, 38),
    ]
    d.line(wave, fill=_ICON_WAVE, width=3, joint="curve")
    # 基线（暗绿）
    d.line([(10, 50), (54, 50)], fill=_ICON_DIM, width=2)
    # 右上角圆点
    d.ellipse([49, 11, 57, 19], fill=_ICON_DOT)
    return img


def _ensure_icon_png() -> Path:
    """图标落盘 %TEMP% 并缓存；文件缺失/损坏时重建，返回 PNG 路径。"""
    if ICON_PATH.is_file():
        try:
            with Image.open(ICON_PATH) as probe:
                probe.verify()
            return ICON_PATH
        except Exception:  # noqa: BLE001 —— 损坏则重建
            pass
    ICON_PATH.parent.mkdir(parents=True, exist_ok=True)
    _build_icon_image().save(ICON_PATH, "PNG")
    return ICON_PATH


# ---------------------------------------------------------------------------
# 窗口枚举与前置（pywin32；手法同 focus 模块）
# ---------------------------------------------------------------------------
def _module_window_prefix(code: str) -> str:
    return f"uptime - {code}"


def _list_module_windows(code: str) -> list[tuple[int, str]]:
    """可见顶层窗口中标题以 "uptime - <代号>" 开头的 [(hwnd, title)]。

    用 strip+前缀容错（终端宿主可能在标题后追加后缀）；枚举瞬间窗口销毁不中断。
    """
    prefix = _module_window_prefix(code)
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


def _force_foreground(hwnd: int) -> bool:
    """前置窗口：SetForegroundWindow 被前台锁拒绝时走 AttachThreadInput + 模拟 Alt。"""
    _dbg(f"force_fg enter hwnd={hwnd} tid={_kernel32.GetCurrentThreadId()}")  # 临时诊断
    ok = _force_foreground_inner(hwnd)
    _dbg(f"force_fg exit hwnd={hwnd} ok={ok}")  # 临时诊断
    return ok


def _force_foreground_inner(hwnd: int) -> bool:
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        return True
    except pywintypes.error:
        pass
    fg = _user32.GetForegroundWindow()
    fg_tid = _user32.GetWindowThreadProcessId(fg, None)
    this_tid = _kernel32.GetCurrentThreadId()
    attached = False
    try:
        if fg_tid and fg_tid != this_tid:
            attached = bool(_user32.AttachThreadInput(this_tid, fg_tid, True))
        _user32.keybd_event(VK_MENU, 0, 0, 0)
        _user32.keybd_event(VK_MENU, 0, 2, 0)  # KEYEVENTF_KEYUP
        try:
            win32gui.SetForegroundWindow(hwnd)
            return True
        except pywintypes.error:
            try:
                win32gui.BringWindowToTop(hwnd)
                return True
            except pywintypes.error:
                return False
    finally:
        if attached:
            _user32.AttachThreadInput(this_tid, fg_tid, False)


def _foreground_module(code: str) -> bool:
    """前置第一个命中的模块窗口；找不到窗口返回 False。"""
    windows = _list_module_windows(code)
    if not windows:
        return False
    return _force_foreground(windows[0][0])


# ---------------------------------------------------------------------------
# 模块启动 / 状态（菜单与热键共用的唯一动作路径 dispatch）
# ---------------------------------------------------------------------------
# 本壳启动的子进程表：code -> Popen（cmd 包装进程，模块运行期间一直存活）
_procs: dict[str, subprocess.Popen] = {}
_state_lock = threading.Lock()


def _spawn(code: str) -> subprocess.Popen:
    """新控制台窗口启动模块。

    cmd /c 三段式：title 先立标题（不依赖 OSC/VT，任何宿主都生效）→
    运行模块 → 退出码非 0 时 pause（异常窗口停留可看错误，不闪退）。
    cmd 与模块同住一个新控制台；cmd 活到模块结束，Popen 存活即模块存活。
    """
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    # 关键：剥掉 PyInstaller 注入的解包标记，否则子 exe 的引导层会校验父进程
    # （父是 cmd.exe ≠ 本 exe）报 "parent process has different executable" 直接拒启
    for k in [k for k in env if k.startswith("_MEIPASS") or k.startswith("_PYI")]:
        env.pop(k)
    title = _module_window_prefix(code)
    cmd_exe = os.environ.get("ComSpec", "cmd.exe")
    if getattr(sys, "frozen", False):
        # 打包形态：本 exe 即入口，uptime.exe <代号> 多路复用（见 uptime/__main__.py）
        run = f'"{sys.executable}" {code}'
    else:
        run = f'"{sys.executable}" -m uptime.{code}'
    line = (
        f'"{cmd_exe}" /c title {title} '
        f"& {run} "
        f"& if errorlevel 1 pause"
    )
    proc = subprocess.Popen(
        line,
        cwd=str(PROJECT_ROOT),
        env=env,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    _dbg(f"spawn {code} pid={proc.pid} line={line!r}")  # 临时诊断
    return proc


def _is_running(code: str) -> bool:
    """本壳启动的该模块子进程是否存活（按 poll 判断）。"""
    proc = _procs.get(code)
    return proc is not None and proc.poll() is None


def dispatch(code: str) -> str:
    """菜单点击 / 热键 / LAUNCH 钩子共用的唯一动作。

    已运行（本壳子进程存活，或桌面已有该模块窗口——如用户手动开的）→ 前置；
    否则新控制台窗口启动。返回 "started" / "foreground" / "foreground-fail"。
    """
    with _state_lock:
        proc = _procs.get(code)
        if proc is not None and proc.poll() is None:
            return "foreground" if _foreground_module(code) else "foreground-fail"
        if _list_module_windows(code):
            # 不是本壳启动但窗口在跑：同样前置，不重复启动
            return "foreground" if _foreground_module(code) else "foreground-fail"
        _procs[code] = _spawn(code)
        return "started"


# ---------------------------------------------------------------------------
# 托盘：菜单 / 图标 / 热键 / 刷新线程
# ---------------------------------------------------------------------------
def _module_text(code: str, show_state: bool):
    """菜单项文本：默认静态代号；show_state 开启时为可调用（打开/刷新菜单时现算）。"""
    if not show_state:
        return code

    def _text(_item: Any) -> str:
        return f"{code} ●" if _is_running(code) else f"{code} ○"

    return _text


def _build_menu(cfg: dict[str, Any]) -> "pystray.Menu":
    """四模块项 + 分隔线 + panel 项 + exit。"""
    show_state = bool(cfg["show_state"])

    def _action(code: str):
        def _on_click(_icon: Any, _item: Any) -> None:
            _dispatch_quiet(code, "menu")

        return _on_click

    def _on_panel(_icon: Any, _item: Any) -> None:
        app = _panel_app
        if app is not None:
            app.show()

    def _on_exit(_icon: Any, _item: Any) -> None:
        _shutdown()  # 停托盘 + 销毁面板 → finally 清理 → 进程退出；不动已启动模块

    items: list[Any] = [
        pystray.MenuItem(_module_text(code, show_state), _action(code))
        for code in MENU_CODES
    ]
    items.append(pystray.Menu.SEPARATOR)
    items.append(pystray.MenuItem("panel", _on_panel))
    items.append(pystray.Menu.SEPARATOR)
    items.append(pystray.MenuItem("exit", _on_exit))
    return pystray.Menu(*items)


def _dispatch_quiet(code: str, source: str) -> None:
    """菜单/热键回调入口：dispatch 异常吞掉打印，保证回调线程不死。"""
    try:
        result = dispatch(code)
        print(f"console: {source} {code} -> {result}", flush=True)
    except Exception as exc:  # noqa: BLE001 —— 回调线程兜底
        print(f"console: {source} {code} failed: {exc!r}", flush=True)


def _register_hotkeys(cfg: dict[str, Any]) -> dict[str, str]:
    """按 console.hotkeys 注册全局热键（行为=dispatch）。空表=一个不注册。失败告警跳过，不崩。"""
    if not cfg["hotkeys"]:
        return {}
    try:
        import keyboard
    except ImportError as exc:
        print(f"console: 缺少 keyboard 库，热键不可用（py -3.10 -m pip install keyboard）：{exc}",
              flush=True)
        return {}
    registered: dict[str, str] = {}
    for code, key in cfg["hotkeys"].items():
        try:
            keyboard.add_hotkey(key, _dispatch_quiet, args=(code, "hotkey"))
            registered[code] = key
        except Exception as exc:  # noqa: BLE001 —— 键位冲突等，优雅降级
            print(f"console: 热键注册失败 {code}={key!r}（{exc}）", flush=True)
    return registered


def _minimize_self() -> None:
    """最小化自身控制台窗口（SW_MINIMIZE：任务栏按钮保留，可找回）。无控制台则跳过。"""
    try:
        hwnd = _kernel32.GetConsoleWindow()
        if hwnd:
            _user32.ShowWindowAsync(hwnd, win32con.SW_MINIMIZE)
    except Exception:  # noqa: BLE001
        pass


def _set_window_title() -> None:
    """窗口标题：OSC 序列 + SetWindowText 双保险（部分控制台宿主不解析 OSC）。"""
    set_console_title(WINDOW_TITLE)
    try:
        hwnd = _kernel32.GetConsoleWindow()
        if hwnd:
            win32gui.SetWindowText(hwnd, WINDOW_TITLE)
    except Exception:  # noqa: BLE001
        pass


def _free_console() -> bool:
    """消灭自身控制台黑窗：GetConsoleWindow 存在才 FreeConsole（最后一个归属
    进程脱离后 conhost 连窗口一起销毁）。无控制台（服务态/管道启动）返回 False。

    打包（onefile）形态：引导进程仍归属该控制台，FreeConsole 后窗口不灭，
    故先 SW_HIDE 彻底隐藏（任务栏按钮同步消失）；源码模式不动用户的终端窗口。
    """
    try:
        hwnd = _kernel32.GetConsoleWindow()
        if hwnd:
            if getattr(sys, "frozen", False):
                _user32.ShowWindowAsync(hwnd, win32con.SW_HIDE)
            _kernel32.FreeConsole()
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _detach_stdio() -> None:
    """FreeConsole 后原 stdout 句柄失效：重定向到 devnull，保证后续 print 安全。"""
    try:
        devnull = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 —— 常驻进程持有到退出
        sys.stdout = devnull
        sys.stderr = devnull
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 壳生命周期：托盘 + 面板统一退出路径（exit 菜单 / 面板退出按钮 / AUTO_EXIT 共用）
# ---------------------------------------------------------------------------
_panel_app: Any = None    # uptime.panel.PanelApp（托盘 panel 项 / 退出路径引用）
_icon_ref: Any = None     # pystray.Icon
_shutdown_done = False


def _shutdown() -> None:
    """整壳干净退出（可从任意线程调）：停托盘 → 请求面板退出 mainloop。幂等。"""
    global _shutdown_done
    if _shutdown_done:
        return
    _shutdown_done = True
    icon = _icon_ref
    if icon is not None:
        try:
            icon.stop()
        except Exception:  # noqa: BLE001
            pass
    app = _panel_app
    if app is not None:
        try:
            app.request_quit()
        except Exception:  # noqa: BLE001
            pass


def _start_menu_refresher(icon: "pystray.Icon") -> threading.Event:
    """show_state 开启时每秒刷新菜单（打开菜单即见最新 ●/○）。返回停止事件。"""
    stop = threading.Event()

    def _loop() -> None:
        while not stop.wait(1.0):
            try:
                icon.update_menu()
            except Exception:  # noqa: BLE001 —— 托盘已停等，安静退出
                return

    threading.Thread(target=_loop, name="console-menu-refresh", daemon=True).start()
    return stop


# ---------------------------------------------------------------------------
# 单实例锁：%TEMP% pid 文件 + msvcrt 非阻塞锁（进程崩溃锁自动释放）
# ---------------------------------------------------------------------------
# 锁区与 pid 文本分区：pid 写 0~31 字节，锁第 64 字节（EOF 外）。
# 不锁字节 0 —— 排他锁会挡住别的实例读 pid 文本（读被锁字节报 PermissionError），
# 分区后 pid 可随时读、锁区永不被读写触碰。
_LOCK_OFFSET = 64
_lock_fh: Any = None


def _acquire_lock() -> tuple[bool, str]:
    """尝试取得实例锁。返回 (是否取得, 已在运行实例的 pid 文本)。

    锁不上=首实例活着；锁文件在且锁得上=旧实例已死（残留文件），接管重写 pid。
    临时目录本身异常时不做单实例保护（放行），不因环境问题拒启动。
    """
    global _lock_fh
    try:
        fh = open(LOCK_PATH, "a+", encoding="utf-8")
        try:
            fh.seek(0)
            try:
                # 裸 fd 精确读 [0,32)：不横跨第 64 字节锁区（缓冲 read 的 OS 级
                # 请求范围达 8K，会碰到锁字节报 PermissionError）
                fd = fh.fileno()
                os.lseek(fd, 0, os.SEEK_SET)
                old_pid = os.read(fd, 32).decode("utf-8", "replace").strip()
            except OSError:
                old_pid = ""
            fh.seek(_LOCK_OFFSET)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                fh.close()
                return False, old_pid
            try:
                fh.truncate(0)
                fh.write(str(os.getpid()))
                fh.flush()
            except OSError:
                pass  # pid 信息只是给人看的，写失败不影响锁
            _lock_fh = fh  # 句柄保持打开＝锁保持到进程退出
            return True, str(os.getpid())
        except OSError:
            fh.close()
            return True, ""
    except OSError:
        return True, ""


def _release_lock() -> None:
    global _lock_fh
    fh = _lock_fh
    _lock_fh = None
    if fh is None:
        return
    try:
        fh.seek(_LOCK_OFFSET)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    try:
        fh.close()
    except OSError:
        pass
    try:
        LOCK_PATH.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 测试钩子模式
# ---------------------------------------------------------------------------
def _dump_label(code: str, show_state: bool) -> str:
    """dump 模式的菜单项文本（与 _module_text 同口径；静态/动态统一现算）。"""
    if not show_state:
        return code
    return f"{code} ●" if _is_running(code) else f"{code} ○"


def _dump_mode(cfg: dict[str, Any]) -> None:
    """结构自检：构建图标/菜单/热键表全部对象，打印结构后退出（不进托盘/面板主循环）。"""
    icon_png = _ensure_icon_png()
    _build_menu(cfg)  # 构建真实菜单对象（副作用校验 pystray 结构合法）
    with Image.open(icon_png) as probe:
        probe.verify()
    lines = [
        f"tooltip: {TOOLTIP}",
        f"window_title: {WINDOW_TITLE}",
        f"menu_items: {len(MENU_CODES) + 2}",
    ]
    for code in MENU_CODES:
        lines.append(f"menu_item: {_dump_label(code, cfg['show_state'])}")
    lines.append("menu_sep: 2")
    lines.append("menu_item: panel")
    lines.append("menu_item: exit")
    for code, key in cfg["hotkeys"].items():
        lines.append(f"hotkey: {code}={key}")
    lines.append(f"icon_png: {icon_png}")
    lines.append("icon_check: ok")
    lines.append(f"show_state: {cfg['show_state']}")
    print("console: dump")
    print("\n".join(lines), flush=True)


def _launch_mode(cfg: dict[str, Any], code: str) -> None:
    """执行一次 dispatch（启动或前置）后：启动了子进程则等它结束再退出。"""
    result = dispatch(code)
    print(f"console: launch {code} -> {result}", flush=True)
    proc = _procs.get(code)
    if proc is not None:
        rc = proc.wait()
        print(f"console: child {code} exited rc={rc}", flush=True)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    mode = os.environ.get("UPTIME_CONSOLE_DUMP")
    launch = os.environ.get("UPTIME_CONSOLE_LAUNCH")
    if launch is not None:
        if launch not in MODULE_CODES:
            print(f"console: UPTIME_CONSOLE_LAUNCH 取值无效：{launch!r}（可选 {'/'.join(MODULE_CODES)}）")
            sys.exit(1)

    # 窗口标题：dump 模式除外（保持 stdout 纯净供解析；其余模式 OSC+SetWindowText）
    if mode is None:
        _set_window_title()

    try:
        cfg = _console_config()
    except ValueError as exc:
        print(f"console: 配置错误：{exc}")
        sys.exit(1)

    if pystray is None or Image is None:
        print("console: 缺少 pystray/Pillow（py -3.10 -m pip install pystray Pillow）")
        sys.exit(1)

    # ---- 测试钩子模式（不进托盘，均不占实例锁）----
    if mode is not None:
        _dump_mode(cfg)
        return
    if launch is not None:
        _launch_mode(cfg, launch)
        return

    # ---- 托盘 + 面板常驻模式 ----
    ok, other_pid = _acquire_lock()
    if not ok:
        # 二次双击不再是失败：请求正在运行的实例弹出面板后正常退出
        try:
            SHOW_PANEL_FLAG.write_text("show\n", encoding="utf-8")
        except OSError:
            pass
        print(f"console: 已在运行 (pid={other_pid or 'unknown'})，已请求显示面板")
        sys.exit(0)

    global _panel_app, _icon_ref
    from uptime.panel import PanelApp  # 延迟导入（panel 反向复用本模块 dispatch）

    icon_png = _ensure_icon_png()
    icon = pystray.Icon(
        TOOLTIP, icon=_build_icon_image(), title=TOOLTIP, menu=_build_menu(cfg)
    )
    _icon_ref = icon
    registered = _register_hotkeys(cfg)

    stop_refresh: threading.Event | None = None
    if cfg["show_state"]:
        stop_refresh = _start_menu_refresher(icon)

    secs = auto_exit_seconds()
    if secs is not None:
        print(f"console: auto exit {secs}s", flush=True)
        threading.Timer(secs, _shutdown).start()

    hotkey_desc = ", ".join(f"{c}={k}" for c, k in registered.items()) or "none"
    print(
        f"console: ready (icon={icon_png.name}, panel=on, hotkeys: {hotkey_desc})",
        flush=True,
    )

    # 消灭自身控制台黑窗（双击 exe 场景）；拿不到控制台则按配置最小化兜底
    if _free_console():
        _detach_stdio()
    elif cfg["minimize_self"]:
        _minimize_self()

    panel = PanelApp(cfg, icon=icon, on_shell_exit=_shutdown)
    _panel_app = panel
    if not cfg["panel_on_start"]:
        panel.start_hidden()  # 只进托盘，面板不显示

    try:
        icon.run_detached()  # 托盘走独立线程；主线程给 tkinter mainloop
        panel.run()          # 阻塞至面板销毁（exit / AUTO_EXIT / close_to_tray=false 的 X）
    finally:
        _shutdown()
        try:
            icon.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            import keyboard

            keyboard.unhook_all()
        except Exception:  # noqa: BLE001
            pass
        if stop_refresh is not None:
            stop_refresh.set()
        _release_lock()
