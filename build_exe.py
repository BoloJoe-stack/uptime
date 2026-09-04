"""打包 uptime 为单文件 exe（PyInstaller）。

用法：py -3.10 build_exe.py（在仓库根执行）
产物：dist/uptime.exe——托盘壳（不带参数）+ 子模块多路复用（uptime.exe burn 等）。
图标复用 console 的程序化绘制；data/ 与 config.example.json 一并打入；
真实 config.json 不打入，首跑在 %APPDATA%\\uptime\\ 自动生成（桌面/exe 旁不留配置，见 common/config.py）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> int:
    # 1) exe 图标：复用 console 的程序化图标，多尺寸写 .ico（build/ 已 gitignore）
    from uptime.console import _build_icon_image

    build_dir = ROOT / "build"
    build_dir.mkdir(exist_ok=True)
    ico = build_dir / "uptime.ico"
    _build_icon_image().save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)])
    print(f"icon -> {ico}")

    # 2) PyInstaller 单文件：走仓库内定制的 uptime.spec（含瘦身 excludes + 去 PIL 冗余二进制）
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           str(ROOT / "uptime.spec")]
    print(" ".join(cmd))
    return subprocess.run(cmd, cwd=str(ROOT)).returncode


if __name__ == "__main__":
    raise SystemExit(main())
