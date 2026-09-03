"""uptime 总入口（`py -3.10 -m uptime` 或打包后的 uptime.exe）。

不带参数 = console 托盘壳；`uptime <代号>` = 直接运行对应模块
（打包形态下，托盘壳拉起子模块新窗口用的就是 `uptime.exe <代号>`）。
"""

from __future__ import annotations

import runpy
import sys


def _dbg(msg: str) -> None:  # 临时诊断：子模块生死追踪
    try:
        import time as _t

        from pathlib import Path
        import tempfile

        p = Path(tempfile.gettempdir()) / "uptime_panel_dbg.log"
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{_t.strftime('%H:%M:%S')} [child] {msg}\n")
    except Exception:  # noqa: BLE001
        pass


_CODES = ("burn", "eta", "tail", "focus", "console")


def main() -> int:
    code = sys.argv[1] if len(sys.argv) > 1 else "console"
    if code not in _CODES:
        print(f"未知模块代号：{code!r}（可选：{' '.join(_CODES)}）")
        return 1
    # 让子模块看到干净的 argv（去掉多路复用用的代号）
    sys.argv = [sys.argv[0], *sys.argv[2:]]
    _dbg(f"start {code} frozen={bool(getattr(sys, 'frozen', False))} "
         f"argv={sys.argv} utf8={sys.flags.utf8_mode}")
    try:
        runpy.run_module(f"uptime.{code}", run_name="__main__", alter_sys=True)
    except BaseException as exc:  # noqa: BLE001 —— 诊断需要抓一切
        _dbg(f"{code} CRASH {exc!r}")
        raise
    _dbg(f"{code} normal exit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
