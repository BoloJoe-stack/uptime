"""`py -3.10 -m uptime.burn` / `uptime.exe burn` —— 桌面挂件入口（美式动漫风）。"""

from __future__ import annotations

import tkinter as tk

from uptime.common.config import load_config
from uptime.widget.burn import BurnWidget


def main() -> None:
    root = tk.Tk()
    root.withdraw()  # 挂件用 Toplevel，主根只做承载
    root.title("uptime - burn")
    BurnWidget(root, load_config(), on_close=lambda _code: root.destroy())
    root.mainloop()


if __name__ == "__main__":
    main()
