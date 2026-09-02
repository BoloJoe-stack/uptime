"""boids · 群集算法演示（flocking simulation，终端全窗低 CPU 常显）。

画面：终端全窗"水体"——5~9 个字符 agent（多种大小/朝向字形，互为镜像），
带惯性巡游（速度向量 + 随机转向扰动，轻微回正到水平方向）；
上升粒子从水体底部或 agent 前端产生，到顶消失，上升快于巡游；
底部 2~4 株摆动植物，相位随时间推进。
页眉一行算法 demo 风说明，页脚统计行：agents 数 / fps / 运行时长。

行为约定：
- 边界处理 = 反弹（越界夹回并镜像速度分量），本模块全程一致；
- rich Live 整帧重绘（auto_refresh=False，主循环手动 refresh 精确配速），
  目标 10~15 fps（默认 12），帧间 pacing sleep，控制 CPU 占用；
- 终端尺寸每帧重读，突变时下一帧重算布局（agent 夹回界内、植物重排）；
- 非 TTY（管道/重定向）：输出单帧静态画面后正常退出（与 burn/eta 一致），
  UPTIME_AUTO_EXIT 照常生效（睡到期限再退）。

配置与测试钩子（config 覆盖默认，容忍缺段/非法值，不修改 config 文件）：
- config["boids"] = {"fps": 12, "fish": 7}   # fps 夹到 10~15，fish 夹到 5~9
- UPTIME_AUTO_EXIT=N                          # 到点干净退出
- Simulation / build_frame / resolve_settings 模块级可导入，供 QC 断言
"""

from __future__ import annotations

import math
import random
import sys
import time
from collections import deque
from dataclasses import dataclass

from rich.console import Console
from rich.live import Live
from rich.text import Text

from uptime.common import auto_exit_seconds, load_config, set_console_title

# 对外伪装标题（ASCII 连字符）
WINDOW_TITLE = "uptime - boids"
# 页眉：算法 demo 风说明（界面内展示）
HEADER_TEXT = "boids — flocking simulation"

# ---------------------------------------------------------------------------
# 默认参数与范围
# ---------------------------------------------------------------------------
DEFAULT_FPS = 12.0
DEFAULT_AGENTS = 7
FPS_RANGE = (10.0, 15.0)      # 目标帧率规格范围
AGENT_RANGE = (5, 9)          # agent 数量规格范围

# 巡游运动：速度向量 = (角度, 速率)；随机转向扰动 + 轻微水平回正 = 惯性巡游
SPEED_RANGE = (3.0, 7.0)      # 单元格/秒
SPEED_JITTER = 2.5            # 速率随机游走强度（单元格/秒^2）
TURN_RATE = 1.8               # 最大转向扰动（弧度/秒）
LEVEL_PULL = 0.6              # 向水平方向的回正系数（1/秒）

# 上升粒子：速度全程快于 agent 巡游（8~14 > 3~7）
PARTICLE_SPEED_RANGE = (8.0, 14.0)
PARTICLE_CAP = 30
BOTTOM_SPAWN_RATE = 1.5       # 底部产生频率（个/秒，泊松近似）
MOUTH_SPAWN_RATE = 1.0        # agent 前端产生频率
PARTICLE_STYLE = "bright_black"

# 摆动植物
PLANT_COUNT_RANGE = (2, 4)
PLANT_HEIGHT_RANGE = (2, 8)
PLANT_WAVE = 0.9              # 相邻行的相位差（摆动波形）

# agent 字形：键 = (尺寸档, 朝向)，朝向 -1 左行 / +1 右行，互为镜像
GLYPHS = {
    (0, -1): "<><",
    (0, 1): "><>",
    (1, -1): "<-))><",
    (1, 1): "><((->",
    (2, -1): "<o))))><",
    (2, 1): "><((((o>",
}
# 同尺寸档两朝向字形等长（3/6/8），反弹边界计算与朝向无关
GLYPH_LEN = {0: 3, 1: 6, 2: 8}

AGENT_STYLES = (
    "cyan", "bright_cyan", "yellow", "bright_yellow",
    "green", "bright_green", "magenta",
)


@dataclass
class BoidsSettings:
    """运行参数（config["boids"] > 默认值）。"""

    fps: float = DEFAULT_FPS
    agents: int = DEFAULT_AGENTS


def resolve_settings(cfg: dict) -> BoidsSettings:
    """合并 config["boids"] 覆盖（容忍缺段/非法值），并夹到规格范围。"""
    settings = BoidsSettings()
    section = cfg.get("boids")
    if isinstance(section, dict):
        raw = section.get("fps")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
            settings.fps = float(raw)
        raw = section.get("fish")
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
            settings.agents = int(raw)
    settings.fps = min(max(settings.fps, FPS_RANGE[0]), FPS_RANGE[1])
    settings.agents = min(max(settings.agents, AGENT_RANGE[0]), AGENT_RANGE[1])
    return settings


# ---------------------------------------------------------------------------
# 模拟实体
# ---------------------------------------------------------------------------
class Boid:
    """单个 agent：位置 + 速度向量（角度/速率），随机转向扰动实现惯性巡游。"""

    __slots__ = ("x", "y", "angle", "speed", "size", "style")

    def __init__(self, width: int, rows: int) -> None:
        self.size = random.choice((0, 1, 2))
        self.style = random.choice(AGENT_STYLES)
        # 初始角度偏向水平巡游
        self.angle = random.choice((0.0, math.pi)) + random.uniform(-0.5, 0.5)
        self.speed = random.uniform(*SPEED_RANGE)
        max_x = float(max(width - GLYPH_LEN[self.size], 1))
        self.x = random.uniform(0.0, max_x)
        self.y = random.uniform(0.0, float(max(rows - 1, 0)))

    def step(self, dt: float, width: int, rows: int) -> None:
        """推进一步；边界=反弹：越界夹回并镜像对应速度分量。"""
        # 随机转向扰动：小步漂移，保留原方向（惯性）
        self.angle += random.uniform(-1.0, 1.0) * TURN_RATE * dt
        # 轻微回正到水平巡游（保留上下摆动）
        target = 0.0 if math.cos(self.angle) >= 0.0 else math.pi
        delta = (target - self.angle + math.pi) % math.tau - math.pi
        self.angle += delta * LEVEL_PULL * dt
        # 速率小幅随机游走
        self.speed += random.uniform(-1.0, 1.0) * SPEED_JITTER * dt
        self.speed = min(max(self.speed, SPEED_RANGE[0]), SPEED_RANGE[1])

        self.x += math.cos(self.angle) * self.speed * dt
        self.y += math.sin(self.angle) * self.speed * dt

        # 反弹边界（全程一致）；字形起点需留在 [0, width - 字形长]
        limit_x = float(max(width - GLYPH_LEN[self.size], 0))
        if self.x < 0.0:
            self.x = 0.0
            self.angle = math.pi - self.angle
        elif self.x > limit_x:
            self.x = limit_x
            self.angle = math.pi - self.angle
        limit_y = float(max(rows - 1, 0))
        if self.y < 0.0:
            self.y = 0.0
            self.angle = -self.angle
        elif self.y > limit_y:
            self.y = limit_y
            self.angle = -self.angle


class Particle:
    """上升粒子：从水体底部或 agent 前端产生，匀速上升，到顶消失。"""

    __slots__ = ("x", "y", "speed", "char")

    def __init__(self, x: float, y: float) -> None:
        self.x = x
        self.y = y
        self.speed = random.uniform(*PARTICLE_SPEED_RANGE)
        self.char = random.choice(("o", "o", "o", "."))

    def step(self, dt: float) -> None:
        self.y -= self.speed * dt


class Plant:
    """底部摆动植物：相位随时间推进，越高摆幅越大。"""

    __slots__ = ("base_x", "height", "phase", "rate", "style")

    def __init__(self, width: int, rows: int) -> None:
        hi = max(min(PLANT_HEIGHT_RANGE[1], max(rows, 1)), 1)
        lo = min(PLANT_HEIGHT_RANGE[0], hi)
        self.height = random.randint(lo, hi)
        self.base_x = random.randint(1, max(width - 2, 2))
        self.phase = random.uniform(0.0, math.tau)
        self.rate = random.uniform(1.0, 2.0)
        self.style = random.choice(("green", "bright_green"))

    def step(self, dt: float) -> None:
        self.phase += self.rate * dt


# ---------------------------------------------------------------------------
# 模拟整体：尺寸自适应 + 实体推进
# ---------------------------------------------------------------------------
class Simulation:
    """水体状态：agent / 植物 / 粒子。advance(dt) 后所有实体保证在界内。"""

    def __init__(self, settings: BoidsSettings, width: int, height: int) -> None:
        self.settings = settings
        self.width = 1
        self.height = 1
        self.water_rows = 1
        self.boids: list[Boid] = []
        self.plants: list[Plant] = []
        self.particles: list[Particle] = []
        self.resize(width, height)
        # 预置几颗粒子，静态单帧也有层次
        for _ in range(3):
            x = random.uniform(0.0, float(max(self.width - 1, 0)))
            y = random.uniform(1.0, float(max(self.water_rows - 1, 1)))
            self.particles.append(Particle(x, y))

    def resize(self, width: int, height: int) -> None:
        """重算布局：水体行数 = 终端高 - 页眉/页脚/余量；agent 夹回界内，植物重排。"""
        self.width = max(int(width), 4)
        self.height = max(int(height), 4)
        self.water_rows = max(self.height - 3, 1)
        if not self.boids:
            self.boids = [Boid(self.width, self.water_rows)
                          for _ in range(self.settings.agents)]
        else:
            for b in self.boids:
                b.x = min(max(b.x, 0.0), float(max(self.width - GLYPH_LEN[b.size], 0)))
                b.y = min(max(b.y, 0.0), float(max(self.water_rows - 1, 0)))
        self.plants = [Plant(self.width, self.water_rows)
                       for _ in range(random.randint(*PLANT_COUNT_RANGE))]
        self.particles = [p for p in self.particles
                          if 0.0 <= p.x < self.width and 0.0 <= p.y < self.water_rows]

    def advance(self, dt: float) -> None:
        """推进一个固定步长：agent / 植物 / 粒子 + 粒子产生与消亡。"""
        for b in self.boids:
            b.step(dt, self.width, self.water_rows)
        for plant in self.plants:
            plant.step(dt)

        # 底部新粒子
        if len(self.particles) < PARTICLE_CAP and random.random() < dt * BOTTOM_SPAWN_RATE:
            x = random.uniform(0.0, float(max(self.width - 1, 0)))
            self.particles.append(Particle(x, float(max(self.water_rows - 1, 0))))
        # agent 前端（口部）新粒子
        if self.boids and len(self.particles) < PARTICLE_CAP \
                and random.random() < dt * MOUTH_SPAWN_RATE:
            b = random.choice(self.boids)
            head_x = b.x + (GLYPH_LEN[b.size] if math.cos(b.angle) >= 0.0 else -1.0)
            head_x = min(max(head_x, 0.0), float(max(self.width - 1, 0)))
            self.particles.append(Particle(head_x, max(b.y, 0.0)))

        alive: list[Particle] = []
        for p in self.particles:
            p.step(dt)
            if p.y >= 0.0:
                alive.append(p)
        self.particles = alive


# ---------------------------------------------------------------------------
# 渲染：整帧字符网格 → 带样式 Text（同风格连续段合并追加，控制开销）
# ---------------------------------------------------------------------------
def _append_row(text: Text, row: list[str], styles: list) -> None:
    """把一整行字符按连续同样式段合并追加进 Text。

    行尾空格全部裁掉（空行不追加任何内容）：尾随空白只会给 rich
    渲染管线增加无效 segments，裁掉可显著降低每帧 CPU 开销。
    """
    limit = len(row)
    while limit > 0 and row[limit - 1] == " ":
        limit -= 1
    if limit == 0:
        return
    run: list[str] = []
    run_style = styles[0]
    for i in range(limit):
        ch, st = row[i], styles[i]
        if st != run_style:
            text.append("".join(run), style=run_style or "")
            run = []
            run_style = st
        run.append(ch)
    text.append("".join(run), style=run_style or "")


def _format_duration(seconds: float) -> str:
    """运行时长 HH:MM:SS。"""
    total = max(int(seconds), 0)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def build_frame(sim: Simulation, *, fps: float, elapsed: float) -> Text:
    """组装一帧（单个 Text）：页眉 / 水体（植物→agent→粒子叠加）/ 页脚统计行。

    单 Text 而非 Group 套多子对象：少一层 renderable 分发，整帧渲染更省。
    """
    w, rows = sim.width, sim.water_rows
    chars = [[" "] * w for _ in range(rows)]
    styles: list[list] = [[None] * w for _ in range(rows)]

    def put(x: int, y: int, glyph: str, style: str) -> None:
        if 0 <= y < rows:
            row, st = chars[y], styles[y]
            for i, ch in enumerate(glyph):
                cx = x + i
                if 0 <= cx < w:
                    row[cx] = ch
                    st[cx] = style

    # 摆动植物（先画，作背景）：相位推进 + 越高摆幅越大
    for plant in sim.plants:
        for i in range(plant.height):
            y = rows - 1 - i
            amp = 0.3 + 2.0 * i / plant.height
            sway = math.sin(plant.phase + i * PLANT_WAVE)
            slope = math.cos(plant.phase + i * PLANT_WAVE)
            x = plant.base_x + int(round(amp * sway))
            glyph = "(" if slope < -0.3 else ")" if slope > 0.3 else "|"
            put(x, y, glyph, plant.style)

    # agent：按当前水平速度分量选朝向字形
    for b in sim.boids:
        direction = 1 if math.cos(b.angle) >= 0.0 else -1
        put(int(round(b.x)), int(round(b.y)), GLYPHS[(b.size, direction)], b.style)

    # 上升粒子（最后画，在前景）
    for p in sim.particles:
        put(int(round(p.x)), int(round(p.y)), p.char, PARTICLE_STYLE)

    frame = Text()
    frame.append(HEADER_TEXT, style="bold cyan")
    frame.append("\n")
    for y in range(rows):
        _append_row(frame, chars[y], styles[y])
        frame.append("\n")
    frame.append(
        f"agents {len(sim.boids)} · fps {fps:.1f} · uptime {_format_duration(elapsed)}",
        style="dim",
    )
    return frame


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def _enable_vt_processing() -> None:
    """Windows 下给 stdout 控制台启用 VT 处理（ENABLE_VIRTUAL_TERMINAL_PROCESSING）。

    conhost 默认不启用 VT：rich 检测不到 VT 就退到 legacy win32 渲染器
    （逐行 FillConsoleOutput* 刷屏，每帧上百次 conhost 调用，CPU 超标）；
    启用后 rich 走 ANSI 写路径。非 Windows / 非 TTY / 调用失败一律静默跳过。
    """
    if sys.platform != "win32" or not sys.stdout.isatty():
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(wintypes.DWORD(0xFFFFFFF5))  # STD_OUTPUT_HANDLE
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        kernel32.SetConsoleMode(handle, wintypes.DWORD(mode.value | 0x0004))
    except Exception:
        pass


def main() -> None:
    _enable_vt_processing()
    cfg = load_config()
    settings = resolve_settings(cfg)
    console = Console()
    sim = Simulation(settings, console.size.width, console.size.height)

    exit_after = auto_exit_seconds()
    deadline = (time.monotonic() + exit_after) if exit_after is not None else None

    if not console.is_terminal:
        # 非终端（管道/重定向）：单帧静态画面后正常退出（与 burn/eta 一致）；
        # 设了 UPTIME_AUTO_EXIT 则睡到期限再退，照常生效
        console.print(build_frame(sim, fps=settings.fps, elapsed=0.0))
        if deadline is not None:
            time.sleep(max(deadline - time.monotonic(), 0.0))
        return

    set_console_title(WINDOW_TITLE)

    dt = 1.0 / settings.fps
    start = time.monotonic()
    frame_times: deque[float] = deque()
    last_size = (console.size.width, console.size.height)

    try:
        initial = build_frame(sim, fps=settings.fps, elapsed=0.0)
        with Live(initial, console=console, auto_refresh=False, screen=False) as live:
            next_tick = time.monotonic()
            while True:
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    break
                # 尺寸自适应：每帧重读，突变则下一帧重算布局
                size = (console.size.width, console.size.height)
                if size != last_size:
                    sim.resize(size[0], size[1])
                    last_size = size
                sim.advance(dt)

                # 实测帧率：最近 2 秒滚动窗口
                frame_times.append(now)
                while frame_times and now - frame_times[0] > 2.0:
                    frame_times.popleft()
                if len(frame_times) >= 2:
                    span = frame_times[-1] - frame_times[0]
                    fps_display = (len(frame_times) - 1) / span if span > 0 else settings.fps
                else:
                    fps_display = settings.fps

                live.update(build_frame(sim, fps=fps_display, elapsed=now - start))
                live.refresh()

                # 帧间配速；不越过退出期限；落后过多则重同步
                next_tick += dt
                nap = next_tick - time.monotonic()
                if nap <= 0.0:
                    next_tick = time.monotonic()
                    continue
                if deadline is not None:
                    nap = min(nap, max(deadline - time.monotonic(), 0.0))
                time.sleep(nap)
    except KeyboardInterrupt:
        pass
