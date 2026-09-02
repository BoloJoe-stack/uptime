"""less · 日志分页器观感的终端视图（对外形象=在 less 里翻服务日志排查）。

双模式（单一 rich Live，切换只换渲染对象，不 clear 不重开 Live）：
- pager（默认）：约 2~4 行/秒慢速滚动的服务日志（时间戳+级别+动态数字文案），
  底部 less 风格状态栏（文件名/行号窗口/百分比，随滚动推进）。
- board（Tab 切入，Esc 切回）：网格巡航视图——方向键/WASD 控制、采集目标(*)、
  计点 n:、撞界或撞身 halted、R 重开、P 暂停；状态在切回 pager 期间保留。
  反转向输入按经典规则忽略（不能 180 度掉头；同一刻连按多键经待向队列逐步生效，
  无法拼出瞬间掉头）。

输入：msvcrt 非阻塞读键（Windows 原生）；管道/无控制台时 kbhit 恒为 False，不报错。
节奏：board tick 10 次/秒（8~12 区间内）。

测试钩子：
- UPTIME_AUTO_EXIT=N     到点干净退出
- UPTIME_LESS_KEYS="..."  按 tick 顺序逐个注入按键（w/a/s/d/p/r/tab/esc；
                          兼容字面 "tab"/"esc" 子串与 \t/\x1b 控制字符），注入完维持当前状态。
                          注意：方向/w/a/s/d 与 p/r 只在 board 模式生效——默认启动是 pager，
                          序列须以 "tab" 开头先切入 board（或改设 UPTIME_LESS_AUTO=1 直接
                          进入 board），否则方向键全部无效。例如字面 "dddddddddd" 会一直
                          停在 pager 毫无效果；要复现"持续右移撞墙"应写 "tabdddddddddd"
- UPTIME_LESS_AUTO=1      自动巡航（贪心追目标+基本避身），并直接进入 board
- UPTIME_SEED=整数         固定随机种子（board 侧随机流独立，目标位置可复现）
"""

from __future__ import annotations

import io
import os
import random
import sys
import time
from collections import deque
from datetime import datetime

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.text import Text

from uptime.common import auto_exit_seconds, get_now, load_config, set_console_title

try:
    import msvcrt
except ImportError:  # 非 Windows 环境，静默降级为无键盘输入
    msvcrt = None

# 对外伪装标题（ASCII 连字符）与 board 页眉
WINDOW_TITLE = "uptime - less"
PANEL_TITLE = "uptime — less"
SUB_TITLE = "debug view"

# 节奏与尺寸
TICK = 0.1                  # board tick 间隔：10 次/秒
BOARD_W = 20
BOARD_H = 12
START_LEN = 4               # 初始身长（含头）
PAGER_VIEW = 14             # pager 视口行数
SCROLL_DELAY_RANGE = (0.25, 0.5)   # 行间延时 → 约 2~4 行/秒

# 方向：屏幕坐标（行向下增）
_DIRS = {"up": (-1, 0), "down": (1, 0), "left": (0, -1), "right": (0, 1)}
# WASD 字母 → 方向名（真实键盘字母与 KEYS 注入字母统一在 _handle 处映射；
# 箭头键路径经 _ARROWS 已产出方向名，两路在此汇合）
_WASD = {"w": "up", "a": "left", "s": "down", "d": "right"}
# msvcrt 扩展键（\x00/\xe0 前缀后跟的字符）→ 方向名
_ARROWS = {"H": "up", "P": "down", "K": "left", "M": "right"}

# pager 伪装文件名轮换
_FILES = ("app.log", "api.log", "worker.log", "jobs.log")
_LEVELS = ("INFO",) * 6 + ("DEBUG",) * 3 + ("WARN",) * 2
_LEVEL_COLOR = {"INFO": "cyan", "DEBUG": "bright_black", "WARN": "yellow"}

_SVCS = ("auth", "gateway", "billing", "scheduler", "uploader", "session", "indexer", "replica")
_OPS = ("refresh", "sync", "apply", "dispatch", "rotate", "verify", "compact", "drain")
# 日志文案模板：数字全部运行时注入
_TEMPLATES = (
    lambda r: f"{r.choice(_SVCS)}: check ok nodes={r.randint(3, 12)} p99={r.uniform(4, 180):.1f}ms",
    lambda r: f"{r.choice(_SVCS)} served qps={r.randint(80, 2400)} err={r.randint(0, 9)}",
    lambda r: f"cache warm keys={r.randint(1200, 88000)} in {r.randint(90, 3200)}ms",
    lambda r: f"db pool in_use={r.randint(1, 28)}/32 waiters={r.randint(0, 4)}",
    lambda r: f"queue events.tasks depth={r.randint(20, 9000)} lag={r.randint(0, 4200)}ms",
    lambda r: f"scan shards {r.randint(2, 15)}/16 matched={r.randint(0, 120)}",
    lambda r: f"gc pause {r.uniform(1.0, 24.0):.1f}ms heap={r.randint(300, 1800)}MB",
    lambda r: f"heartbeat ok rtt={r.uniform(0.3, 40):.1f}ms peers={r.randint(2, 8)}",
    lambda r: f"retry {r.randint(2, 5)}/{r.randint(6, 8)} {r.choice(_SVCS)}.{r.choice(_OPS)}"
              f" backoff={r.randint(40, 900)}ms",
    lambda r: f"compact segment {r.randint(100, 999)} entries={r.randint(1000, 90000)}"
              f" took {r.randint(80, 2400)}ms",
    lambda r: f"rebalance done moved={r.randint(1, 64)} shards in {r.randint(1, 40)}s",
    lambda r: f"snapshot ok size={r.randint(12, 980)}MB crc ok",
)


# ---------------------------------------------------------------------------
# 键盘：msvcrt 非阻塞读键 + 环境变量注入序列解析
# ---------------------------------------------------------------------------
def _poll_keys() -> list[str]:
    """非阻塞读出本 tick 内按下的键名列表；管道/无控制台/非 Windows 返回空。"""
    keys: list[str] = []
    if msvcrt is None or not sys.stdin.isatty():
        return keys
    for _ in range(8):  # 单 tick 最多取 8 键，防异常刷键卡死
        try:
            if not msvcrt.kbhit():
                break
            ch = msvcrt.getwch()
        except OSError:
            break
        if ch in ("\x00", "\xe0"):  # 方向键等扩展键：两段读
            try:
                if not msvcrt.kbhit():
                    break
                ext = msvcrt.getwch()
            except OSError:
                break
            if ext in _ARROWS:
                keys.append(_ARROWS[ext])
            continue
        if ch == "\t":
            keys.append("tab")
        elif ch == "\x1b":
            keys.append("esc")
        elif ch in ("\r", "\n"):
            continue
        else:
            keys.append(ch.lower())
    return keys


def _parse_keys(spec: str) -> list[str]:
    """解析 UPTIME_LESS_KEYS：字面 "tab"/"esc" 子串或 \t/\x1b 控制字符，其余按单字符。"""
    out: list[str] = []
    i = 0
    while i < len(spec):
        three = spec[i : i + 3].lower()
        if three == "tab":
            out.append("tab")
            i += 3
            continue
        if three == "esc":
            out.append("esc")
            i += 3
            continue
        ch = spec[i]
        if ch == "\t":
            out.append("tab")
        elif ch == "\x1b":
            out.append("esc")
        elif ch.lower() in "wasdpr":
            out.append(ch.lower())
        i += 1  # 其余字符（空格等）忽略
    return out


# ---------------------------------------------------------------------------
# pager：慢速滚动日志 + less 风格状态栏
# ---------------------------------------------------------------------------
class _PagerModel:
    """日志视口：按行延时滚动，状态栏行号/百分比随滚动推进，滚到头换下一个文件。"""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.view = PAGER_VIEW
        self.lines: deque[Text] = deque(maxlen=self.view)
        self.acc = 0.0
        self._file_idx = rng.randrange(len(_FILES))
        # 日志模拟时钟（秒，float）：逐行严格递增，避免真实 less 视图里时间戳倒挂穿帮；
        # 初值取 get_now() 回退一点，使首屏末行时间贴近当下（受 UPTIME_FAKE_NOW 覆盖）
        self._ts = get_now().timestamp() - self.view * 2.0
        self._open_file()

    def _open_file(self, jump: float = 0.0) -> None:
        self._ts += jump  # 换文件时模拟时钟整体前跳（后打开的日志文件时间靠后）
        self.filename = _FILES[self._file_idx]
        self.total = self.rng.randint(1600, 3200)
        self.pos = self.view + self.rng.randint(0, 40)  # 已滚到的行号（视口末行）
        self.lines.clear()
        for _ in range(self.view):
            self.lines.append(self._gen_line())
        self.delay = self.rng.uniform(*SCROLL_DELAY_RANGE)

    def _gen_line(self) -> Text:
        rng = self.rng
        level = rng.choice(_LEVELS)
        self._ts += rng.uniform(0.05, 2.0)  # 每行时间戳相对上一行严格递增
        t = datetime.fromtimestamp(self._ts)
        ts = f"{t:%H:%M:%S}.{t.microsecond // 1000:03d}"
        msg = rng.choice(_TEMPLATES)(rng)
        return Text.assemble(
            (ts + " ", "dim"),
            (f"[{level:<5}] ", _LEVEL_COLOR[level]),
            (msg,),
        )

    def advance(self, dt: float) -> None:
        """按累计时间滚动日志行（任意模式下后台持续滚动）。"""
        self.acc += dt
        while self.acc >= self.delay:
            self.acc -= self.delay
            self.lines.append(self._gen_line())
            self.pos += 1
            if self.pos >= self.total:  # 滚到文件尾：换下一个文件
                self._file_idx = (self._file_idx + 1) % len(_FILES)
                self._open_file(jump=self.rng.uniform(600, 5400))
                break
            self.delay = self.rng.uniform(*SCROLL_DELAY_RANGE)

    def frame(self) -> RenderableType:
        top = max(self.pos - self.view + 1, 1)
        pct = min(self.pos * 100 // self.total, 100)
        status = Text(
            f" {self.filename}  lines {top}-{self.pos}/{self.total}  {pct}% ",
            style="bold reverse",
        )
        return Group(*self.lines, status)


# ---------------------------------------------------------------------------
# board：网格巡航视图
# ---------------------------------------------------------------------------
class _BoardModel:
    """网格巡航：头 @、身 o、目标 *；撞界/撞身 halted；R 重开；P 暂停。"""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng  # board 专属随机流（与 pager 分离，保证 SEED 下目标位置可复现）
        self.reset()

    def reset(self) -> None:
        head_r, head_c = BOARD_H // 2, BOARD_W // 2
        self.body: deque[tuple[int, int]] = deque(
            (head_r, head_c - i) for i in range(START_LEN)  # 头在前，身向左延伸
        )
        self.dir = _DIRS["right"]
        self._pending: deque[tuple[int, int]] = deque()  # 待向队列：每步只消费一个
        self.score = 0
        self.steps = 0
        self.paused = False
        self.dead = False
        self._spawn_food()

    def _spawn_food(self) -> None:
        occupied = set(self.body)
        while True:
            pos = (self.rng.randrange(BOARD_H), self.rng.randrange(BOARD_W))
            if pos not in occupied:
                self.food = pos
                return

    def steer(self, d: tuple[int, int]) -> None:
        """转向入队（至多缓存 3 个），每个步进只消费一个。

        入队时相对『上一生效方向』（队尾或当前方向）做经典校验：同向与反向
        （180 度掉头）直接忽略。因此同一 tick 内先后到达的两个转向会分两步生效
        （如右行时连按上+左：先上一步、再左一步），无法瞬间掉头撞自己。
        """
        last = self._pending[-1] if self._pending else self.dir
        if d == last or d == (-last[0], -last[1]):
            return
        if len(self._pending) < 3:
            self._pending.append((d[0], d[1]))

    def autopilot_dir(self) -> tuple[int, int] | None:
        """自动巡航：贪心追目标（曼哈顿距离最近）+ 基本避身避界；无路可走返回 None。"""
        head = self.body[0]
        best, best_key = None, None
        body_set = set(self.body)
        for d in _DIRS.values():
            if d == (-self.dir[0], -self.dir[1]):  # 排除掉头方向
                continue
            nr, nc = head[0] + d[0], head[1] + d[1]
            if not (0 <= nr < BOARD_H and 0 <= nc < BOARD_W):
                continue
            if (nr, nc) in body_set:  # 保守策略：整条身体都视为占用
                continue
            dist = abs(nr - self.food[0]) + abs(nc - self.food[1])
            key = (dist, d != self.dir)  # 同距优先保持现方向，减少抖动
            if best_key is None or key < best_key:
                best, best_key = d, key
        return best

    def step(self) -> None:
        """推进一格：吃到目标则计点加长，否则尾移；撞界/撞身 halted。"""
        if self.paused or self.dead:
            return
        if self._pending:  # 本步只消费一个待转向；相对当前实际方向的掉头丢弃
            d = self._pending.popleft()
            if d != (-self.dir[0], -self.dir[1]):
                self.dir = d
        head = self.body[0]
        nr, nc = head[0] + self.dir[0], head[1] + self.dir[1]
        if not (0 <= nr < BOARD_H and 0 <= nc < BOARD_W):
            self.dead = True
            return
        eating = (nr, nc) == self.food
        # 未进食时尾尖本步会移走，可视为空位；进食时整条身体都是占用
        occupied = set(self.body) if eating else set(list(self.body)[:-1])
        if (nr, nc) in occupied:
            self.dead = True
            return
        self.body.appendleft((nr, nc))
        if eating:
            self.score += 1
            self._spawn_food()
        else:
            self.body.pop()
        self.steps += 1

    # -- 渲染 ------------------------------------------------------------
    def frame(self) -> RenderableType:
        header = Text.assemble(
            (PANEL_TITLE, "bold cyan"), ("  ·  ", "dim"), (SUB_TITLE, "dim")
        )
        border_top = Text("┌" + "─" * BOARD_W + "┐", style="dim")
        border_bot = Text("└" + "─" * BOARD_W + "┘", style="dim")

        grid = Text(no_wrap=True)
        body_set = set(self.body)
        head = self.body[0]
        for r in range(BOARD_H):
            if r:
                grid.append("\n")
            grid.append("│", "dim")
            for c in range(BOARD_W):
                pos = (r, c)
                if pos == head:
                    grid.append("x" if self.dead else "@",
                                "bold red" if self.dead else "bold cyan")
                elif pos == self.food:
                    grid.append("*", "bold yellow")
                elif pos in body_set:
                    grid.append("o", "green")
                else:
                    grid.append("·", "bright_black")
            grid.append("│", "dim")

        length = len(self.body)
        if self.dead:
            status = Text.assemble(
                (" halted", "bold red"),
                ("  n:", "dim"), (str(self.score), "bold cyan"),
                (f"  len:{length}", "dim"),
                ("  ·  [r] restart · [esc] pager", "dim"),
            )
        elif self.paused:
            status = Text.assemble(
                (" paused", "yellow"),
                ("  n:", "dim"), (str(self.score), "bold cyan"),
                (f"  len:{length}", "dim"),
                ("  ·  [p] resume · [esc] pager", "dim"),
            )
        else:
            status = Text.assemble(
                (" n:", "dim"), (str(self.score), "bold cyan"),
                (f"  len:{length}", "dim"),
                ("  ·  [p] pause · [esc] pager", "dim"),
            )
        return Group(header, border_top, grid, border_bot, status)


# ---------------------------------------------------------------------------
# 应用：模式切换 + 主循环（单一 Live，切换只换渲染对象）
# ---------------------------------------------------------------------------
class _App:
    def __init__(self, seed: int | None, auto: bool, keys: list[str]) -> None:
        self.pager = _PagerModel(random.Random(seed))
        self.board = _BoardModel(random.Random(seed))  # 独立随机流，SEED 下可复现
        self.mode = "board" if auto else "pager"
        self.auto = auto
        self.keys: deque[str] = deque(keys)

    # -- 按键路由 --------------------------------------------------------
    def _handle(self, key: str) -> None:
        if key == "tab":
            if self.mode == "pager":
                self.mode = "board"
        elif key == "esc":
            if self.mode == "board":
                self.mode = "pager"
        elif self.mode == "board":
            board = self.board
            if key == "p":
                board.paused = not board.paused
            elif key == "r":
                if board.dead:
                    board.reset()
            else:
                # w/a/s/d 字母键与箭头键方向名在此统一映射后转向
                dname = _WASD.get(key, key)
                if dname in _DIRS and not board.paused and not board.dead:
                    board.steer(_DIRS[dname])

    # -- 主循环 ------------------------------------------------------------
    def _loop(self, deadline: float | None, emit) -> None:
        next_tick = time.monotonic()
        while True:
            # 1) 输入：先真实按键，后注入键（注入键每 tick 至多一个）
            for key in _poll_keys():
                self._handle(key)
            injected = self.keys.popleft() if self.keys else None
            if injected is not None:
                self._handle(injected)
            # 2) 自动巡航（本 tick 无注入键时接管转向）
            if (
                self.auto
                and injected is None
                and self.mode == "board"
                and not self.board.paused
                and not self.board.dead
            ):
                d = self.board.autopilot_dir()
                if d is not None:
                    self.board.steer(d)
            # 3) 推进：仅 board 模式走 tick；pager 后台持续滚动
            if self.mode == "board":
                self.board.step()
            self.pager.advance(TICK)
            # 4) 渲染（同一渲染管线，切换只换对象）
            emit(self.board.frame() if self.mode == "board" else self.pager.frame())
            # 5) 到点退出
            if deadline is not None and time.monotonic() >= deadline:
                return
            next_tick += TICK
            nap = next_tick - time.monotonic()
            if nap < 0:  # 落后过多则重置基准，避免追赶连发
                next_tick = time.monotonic()
                nap = 0.0
            if deadline is not None:
                nap = min(nap, max(deadline - time.monotonic(), 0.0))
            time.sleep(nap)

    def run(self) -> None:
        exit_after = auto_exit_seconds()
        deadline = time.monotonic() + exit_after if exit_after is not None else None
        if sys.stdout.isatty():
            # 真终端：单一 Live 全程复用，只 update，不 clear 不重开
            with Live(refresh_per_second=12, screen=False) as live:
                self._loop(deadline, live.update)
        else:
            # 管道/重定向：降级为逐 tick 输出纯文本帧（帧间空行分隔），
            # 同一渲染管线，供无头 QC 驱动与比对
            self._loop(deadline, self._print_frame)

    @staticmethod
    def _print_frame(frame: RenderableType) -> None:
        buf = io.StringIO()
        console = Console(
            file=buf, width=110, no_color=True, highlight=False, emoji=False
        )
        console.print(frame)
        sys.stdout.write(buf.getvalue().rstrip("\n") + "\n\n")
        sys.stdout.flush()


def main() -> None:
    load_config()  # 保持各模块启动口径一致（less 无专属配置段）
    set_console_title(WINDOW_TITLE)

    seed: int | None = None
    raw = os.environ.get("UPTIME_SEED")
    if raw is not None:
        try:
            seed = int(raw)
        except ValueError:
            seed = None
    auto = os.environ.get("UPTIME_LESS_AUTO") == "1"
    keys = _parse_keys(os.environ.get("UPTIME_LESS_KEYS", ""))

    app = _App(seed, auto, keys)
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        pass
