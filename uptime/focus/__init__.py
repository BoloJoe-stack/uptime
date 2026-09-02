"""focus · 专注模式开关（全局热键一键隐藏/恢复匹配窗口，对外形象=专注模式）。

按热键＝进入专注：枚举可见顶层窗口，标题命中 hide_patterns 的全部 SW_MINIMIZE
（不关进程，可恢复；排除自身控制台窗口）；show_patterns 非空时把第一个命中的
工作窗口前置（处理前台锁：AttachThreadInput + 模拟 Alt）；被隐藏 HWND 列表与
原状态落盘 %TEMP%/uptime_focus_state.json。
再按一次＝退出专注：按落盘清单恢复全部窗口（IsWindow 校验存活，已关闭的跳过
不报错），清状态。纯开关状态机：0 窗口命中、状态文件缺失/损坏均不报错。

控制台输出极简中性：每次动作一行 "focus: on (3) 12ms" / "focus: off (3) 8ms"；
逐窗口明细与耗时写日志文件（log_file 为空时用 %TEMP%/uptime_focus.log），不上屏。
计时：从动作触发到全部窗口命令派发完成（含状态落盘），输出含毫秒数；
窗口宿主自身的最小化/还原动画耗时另计（最小化/恢复用 ShowWindowAsync 异步派发，
不阻塞 keyboard 事件线程，避免连续热键被拖垮）。

测试钩子（均不触发热键注册）：
- UPTIME_FOCUS_ONCE=hide    不等热键，立即执行一次“进入专注”后退出（状态落盘）
- UPTIME_FOCUS_ONCE=restore 不等热键，立即执行一次“退出专注”后退出
- UPTIME_FOCUS_ONCE=off     干跑自检：枚举+匹配+自身排除全跑但不动任何窗口，
                            输出将命中数与耗时，形如 "focus: dry (2) 3ms"
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pywintypes
import win32con
import win32gui
import win32process

from uptime.common import load_config, set_console_title

WINDOW_TITLE = "uptime - focus"
DEFAULT_HOTKEY = "ctrl+alt+b"
DEFAULT_HIDE_PATTERNS = ["uptime - "]

TEMP_DIR = Path(tempfile.gettempdir())
STATE_FILE = TEMP_DIR / "uptime_focus_state.json"
DEFAULT_LOG_FILE = TEMP_DIR / "uptime_focus.log"

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

SW_MINIMIZE = win32con.SW_MINIMIZE
SW_RESTORE = win32con.SW_RESTORE

VK_MENU = 0x12  # Alt 键：前台锁绕过的经典手法（模拟一次按下/抬起）


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def _str_list(val: Any, key: str) -> list[str]:
    if not isinstance(val, list) or any(not isinstance(x, str) for x in val):
        raise ValueError(f"配置项 focus.{key} 必须是字符串列表，当前为 {val!r}")
    return val


def focus_config() -> dict[str, Any]:
    """读取 focus 配置段（缺段容忍，取内置默认），类型非法抛中文 ValueError。"""
    section = load_config().get("focus") or {}
    if not isinstance(section, dict):
        raise ValueError(f"配置项 focus 必须是 JSON 对象，当前为 {section!r}")
    hotkey = section.get("hotkey", DEFAULT_HOTKEY)
    if not isinstance(hotkey, str) or not hotkey.strip():
        raise ValueError(
            f"配置项 focus.hotkey 必须是非空热键字符串（如 ctrl+alt+b），当前为 {hotkey!r}"
        )
    cfg = {
        "hotkey": hotkey.strip(),
        "hide_patterns": _str_list(
            section.get("hide_patterns", DEFAULT_HIDE_PATTERNS), "hide_patterns"
        ),
        "show_patterns": _str_list(section.get("show_patterns", []), "show_patterns"),
        "log_file": "",
    }
    log_file = section.get("log_file", "")
    if not isinstance(log_file, str):
        raise ValueError(f"配置项 focus.log_file 必须是字符串，当前为 {log_file!r}")
    cfg["log_file"] = log_file
    return cfg


# ---------------------------------------------------------------------------
# 窗口枚举与匹配
# ---------------------------------------------------------------------------
def _list_windows() -> list[tuple[int, str, bool]]:
    """全部可见顶层窗口 [(hwnd, title, iconic)]；空标题跳过，枚举中窗口销毁不中断。"""
    out: list[tuple[int, str, bool]] = []

    def _cb(hwnd: int, _extra: Any) -> bool:
        try:
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if title:
                    out.append((hwnd, title, bool(win32gui.IsIconic(hwnd))))
        except pywintypes.error:
            pass  # 枚举瞬间窗口销毁等，跳过该窗口
        return True

    win32gui.EnumWindows(_cb, None)
    return out


def _is_self(hwnd: int, title: str, console_hwnd: int) -> bool:
    """自身窗口判定：控制台句柄 / 本进程顶层窗口 / 标题以本模块标题开头的终端宿主
    （Windows Terminal 等宿主可能给标题追加后缀或空白，用 strip+前缀容错）。"""
    if not hwnd or hwnd == console_hwnd or title.strip().startswith(WINDOW_TITLE):
        return True
    try:
        _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid == os.getpid():
            return True
    except pywintypes.error:
        pass
    return False


def _match(title: str, patterns: list[str]) -> bool:
    """标题子串匹配：任一非空 pattern 命中即 True。"""
    return any(p and p in title for p in patterns)


def _force_foreground(hwnd: int) -> bool:
    """前置窗口：先直接 SetForegroundWindow，被前台锁拒绝时走
    AttachThreadInput + 模拟 Alt 的成熟手法再试。"""
    try:
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
        # 模拟一次 Alt 按下/抬起，骗过“进程正接收输入”的前台判定
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


# ---------------------------------------------------------------------------
# 状态落盘（%TEMP%/uptime_focus_state.json，缺失/损坏一律视为无状态）
# ---------------------------------------------------------------------------
def _write_state(hidden: list[dict[str, Any]]) -> None:
    data = {
        "version": 1,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "hidden": hidden,
    }
    STATE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def _read_state() -> list[dict[str, Any]]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        entries = data.get("hidden")
        if isinstance(entries, list):
            return [
                e for e in entries
                if isinstance(e, dict) and isinstance(e.get("hwnd"), int)
            ]
    return []


def _clear_state() -> None:
    try:
        STATE_FILE.unlink()
    except (FileNotFoundError, PermissionError):
        pass


# ---------------------------------------------------------------------------
# 日志（逐窗口明细，只写文件不上屏；写失败静默，不影响主流程）
# ---------------------------------------------------------------------------
def _append_log(cfg: dict[str, Any], kind: str, summary: str, notes: list[str]) -> None:
    try:
        path = Path(cfg["log_file"]) if cfg["log_file"] else DEFAULT_LOG_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        lines = [f"{ts} focus {kind}: {summary}"] + [f"  {n}" for n in notes]
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 三个动作：进入专注 / 退出专注 / 干跑（均返回 (计数, 明细行)）
# ---------------------------------------------------------------------------
def _enter(cfg: dict[str, Any]) -> tuple[int, list[str]]:
    """进入专注：最小化全部命中窗口（排除自身、已最小化的跳过），可选前置工作窗口，落盘。"""
    console_hwnd = _kernel32.GetConsoleWindow()
    hidden: list[dict[str, Any]] = []
    notes: list[str] = []
    for hwnd, title, iconic in _list_windows():
        if _is_self(hwnd, title, console_hwnd):
            continue
        if not _match(title, cfg["hide_patterns"]):
            continue
        if iconic:
            notes.append(f"hwnd=0x{hwnd:08X} title={title!r} already minimized, skip")
            continue
        show_cmd = win32gui.GetWindowPlacement(hwnd)[1]
        # ShowWindowAsync：异步派发最小化命令，不做跨进程同步等待——
        # 热键回调在 keyboard 事件线程上执行，同步 ShowWindow 遇到响应慢的
        # 窗口宿主（如终端动画）会阻塞数几百毫秒，拖垮后续热键事件处理
        _user32.ShowWindowAsync(hwnd, SW_MINIMIZE)
        hidden.append({"hwnd": hwnd, "title": title, "show_cmd": show_cmd})
        notes.append(
            f"hwnd=0x{hwnd:08X} title={title!r} minimized (show_cmd={show_cmd})"
        )
    if cfg["show_patterns"]:
        for hwnd, title, iconic in _list_windows():
            if _is_self(hwnd, title, console_hwnd):
                continue
            if _match(title, cfg["show_patterns"]):
                if iconic:
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                ok = _force_foreground(hwnd)
                notes.append(
                    f"foreground hwnd=0x{hwnd:08X} title={title!r} "
                    f"{'ok' if ok else 'fail'}"
                )
                break
    _write_state(hidden)
    return len(hidden), notes


def _exit(cfg: dict[str, Any]) -> tuple[int, list[str]]:
    """退出专注：按落盘清单恢复全部窗口（已关闭的跳过不报错），清状态。"""
    entries = _read_state()
    notes: list[str] = []
    alive = 0
    for e in entries:
        hwnd = int(e["hwnd"])
        title = str(e.get("title", ""))
        if not win32gui.IsWindow(hwnd):
            # 校验存活：已关闭的跳过不报错、不计入恢复数
            notes.append(f"hwnd=0x{hwnd:08X} title={title!r} gone, skip")
            continue
        alive += 1
        if win32gui.IsIconic(hwnd):
            prev = e.get("show_cmd", win32con.SW_SHOWNORMAL)
            cmd = (
                win32con.SW_MAXIMIZE
                if prev == win32con.SW_SHOWMAXIMIZED
                else win32con.SW_RESTORE
            )
            _user32.ShowWindowAsync(hwnd, cmd)
            notes.append(f"hwnd=0x{hwnd:08X} title={title!r} restored (cmd={cmd})")
        else:
            notes.append(f"hwnd=0x{hwnd:08X} title={title!r} already visible, skip")
    _clear_state()
    return alive, notes


def _dry(cfg: dict[str, Any]) -> tuple[int, list[str]]:
    """干跑自检：枚举+匹配+自身排除全跑，不动任何窗口。"""
    console_hwnd = _kernel32.GetConsoleWindow()
    notes: list[str] = []
    hits = 0
    for hwnd, title, iconic in _list_windows():
        if _match(title, cfg["hide_patterns"]):
            if _is_self(hwnd, title, console_hwnd):
                notes.append(f"hwnd=0x{hwnd:08X} title={title!r} self, excluded")
                continue
            hits += 1
            notes.append(
                f"hwnd=0x{hwnd:08X} title={title!r} would minimize (iconic={iconic})"
            )
    return hits, notes


def _run_action(cfg: dict[str, Any], kind: str) -> tuple[int, int]:
    """执行动作并计时（触发→全部窗口处理完成含落盘），明细与耗时写日志。"""
    t0 = time.perf_counter()
    action = {"on": _enter, "off": _exit, "dry": _dry}[kind]
    n, notes = action(cfg)
    ms = int(round((time.perf_counter() - t0) * 1000))
    _append_log(cfg, kind, f"{n} window(s), {ms}ms", notes)
    return n, ms


# ---------------------------------------------------------------------------
# 热键模式与入口
# ---------------------------------------------------------------------------
def _on_hotkey(cfg: dict[str, Any]) -> None:
    """热键回调：按状态文件纯开关切换；异常吞掉记日志，保证钩子不死进程不崩。"""
    try:
        kind = "off" if _read_state() else "on"
        n, ms = _run_action(cfg, kind)
        print(f"focus: {kind} ({n}) {ms}ms", flush=True)
    except Exception as exc:  # noqa: BLE001 —— 回调线程兜底
        try:
            _append_log(cfg, "error", f"hotkey handler failed: {exc!r}", [])
        except Exception:
            pass


def _run_hotkey(cfg: dict[str, Any]) -> None:
    try:
        import keyboard
    except ImportError as exc:
        print(f"focus: 缺少 keyboard 库（py -3.10 -m pip install keyboard）：{exc}")
        sys.exit(1)
    try:
        keyboard.add_hotkey(cfg["hotkey"], _on_hotkey, args=(cfg,))
    except Exception as exc:
        print(f"focus: 热键配置无效：{cfg['hotkey']!r}（{exc}）")
        sys.exit(1)
    print(f"focus: ready ({cfg['hotkey']})", flush=True)
    try:
        keyboard.wait()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            keyboard.unhook_all()
        except Exception:
            pass


def _set_window_title() -> None:
    """窗口标题：OSC 序列 + SetWindowText 双保险（部分控制台宿主不解析 OSC）。"""
    set_console_title(WINDOW_TITLE)
    try:
        hwnd = _kernel32.GetConsoleWindow()
        if hwnd:
            win32gui.SetWindowText(hwnd, WINDOW_TITLE)
    except Exception:
        pass


def main() -> None:
    _set_window_title()
    try:
        cfg = focus_config()
    except ValueError as exc:
        print(f"focus: 配置错误：{exc}")
        sys.exit(1)

    once = os.environ.get("UPTIME_FOCUS_ONCE")
    if once is not None:
        if once not in ("hide", "restore", "off"):
            print(f"focus: UPTIME_FOCUS_ONCE 取值无效：{once!r}（可选 hide/restore/off）")
            sys.exit(1)
        try:
            kind = {"hide": "on", "restore": "off", "off": "dry"}[once]
            n, ms = _run_action(cfg, kind)
            label = {"on": "on", "off": "off", "dry": "dry"}[kind]
            print(f"focus: {label} ({n}) {ms}ms", flush=True)
        except Exception as exc:  # noqa: BLE001 —— 单次动作失败也不裸抛栈
            print(f"focus: 执行失败：{exc}")
            sys.exit(1)
        return

    _run_hotkey(cfg)
