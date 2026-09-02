"""tail · 构建日志流模拟（tail -f 观感，对外形象=盯着构建/调试输出）。

以段落为单位滚动输出：一段同语言代码（8~25 行）→ 数行日志/测试输出 → 下一段。
三种语言（python/js/go）洗牌轮换不扎堆；语料块按洗牌牌堆消费、抽尽才重洗；
日志行的时间戳与数字（重试次数/耗时毫秒/命中率/进度）运行时注入。

节奏：默认约 6~12 行/秒（行间延时抖动 + 偶发快速爆发）；
默认每 30~90 秒随机卡住 3~5 秒（像在断点处思考），恢复后来一小波快刷。

配置与测试钩子（优先级：环境变量 > config["tail"] > 默认）：
- UPTIME_AUTO_EXIT=N            到点干净退出
- UPTIME_TAIL_STALL_EVERY=N     卡顿平均间隔秒数，0=关闭卡顿
- config["tail"] = {"lines_per_sec": 8, "stall_every_sec": 60}
"""

from __future__ import annotations

import os
import random
import re
import sys
import time
from collections import deque
from dataclasses import dataclass

from uptime.common import auto_exit_seconds, get_now, load_config, set_console_title

# 对外伪装标题（ASCII 连字符）
WINDOW_TITLE = "uptime - tail"

# 语料目录：仓库根/data/code_corpus（与 common.config 的根定位方式一致）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORPUS_DIR = os.path.join(PROJECT_ROOT, "data", "code_corpus")
LANGS = ("python", "js", "go")

# 节奏默认值
DEFAULT_LINES_PER_SEC = 8.0
DEFAULT_STALL_EVERY_SEC = 60.0  # 平均间隔；实际采样 ±50%（默认即 30~90 秒）
STALL_DURATION_RANGE = (3.0, 5.0)   # 卡住时长（秒）
BURST_PROB = 0.05                   # 单行触发快速爆发的概率
BURST_LEN_RANGE = (5, 12)           # 爆发持续行数
POST_STALL_BURST_RANGE = (10, 20)   # 卡顿恢复后的快刷行数
DELAY_JITTER = (0.6, 1.5)           # 行间延时倍率抖动
BURST_FACTOR = 0.22                 # 爆发时的延时倍率
LOG_COUNT_PER_SEGMENT = (2, 5)      # 每段代码后的日志/测试行数
TEST_LINE_PROB = 0.30               # 日志行里混入测试输出行的比例

# 日志级别权重：INFO / DEBUG / WARN
_LEVELS = ("INFO", "INFO", "INFO", "INFO", "INFO", "DEBUG", "DEBUG", "DEBUG", "WARN", "WARN")

# ANSI 着色（仅在 TTY 下启用；管道/重定向时输出纯文本，保证流式与可 grep）
_USE_COLOR = sys.stdout.isatty()
_C_RESET = "\x1b[0m"
_C_DIM = "\x1b[2m"
_C_CYAN = "\x1b[36m"
_C_GREEN = "\x1b[32m"
_C_YELLOW = "\x1b[93m"
_LEVEL_COLOR = {"INFO": _C_CYAN, "WARN": _C_YELLOW, "DEBUG": _C_DIM}


class _TimeUp(Exception):
    """UPTIME_AUTO_EXIT 到点，用于干净退出主循环。"""


@dataclass
class TailSettings:
    """运行参数（环境变量 > config["tail"] > 默认值）。"""

    lines_per_sec: float = DEFAULT_LINES_PER_SEC
    stall_every_sec: float = DEFAULT_STALL_EVERY_SEC
    # 间隔采样浮动比例：默认 60s ±50% 即规格的 30~90s；
    # 显式覆盖值用更紧的 ±25%，保证覆盖效果可测
    stall_spread: float = 0.5


def resolve_settings(cfg: dict) -> TailSettings:
    """合并 config["tail"] 与环境变量，容忍缺段/非法值。"""
    settings = TailSettings()

    section = cfg.get("tail")
    if isinstance(section, dict):
        raw = section.get("lines_per_sec")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
            settings.lines_per_sec = float(raw)
        raw = section.get("stall_every_sec")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0:
            settings.stall_every_sec = float(raw)
            settings.stall_spread = 0.25

    # 环境变量只覆盖卡顿间隔（0=关闭）
    raw_env = os.environ.get("UPTIME_TAIL_STALL_EVERY")
    if raw_env is not None:
        try:
            val = int(raw_env)
        except ValueError:
            val = -1
        if val >= 0:
            settings.stall_every_sec = float(val)
            settings.stall_spread = 0.25

    settings.lines_per_sec = min(max(settings.lines_per_sec, 0.5), 60.0)
    settings.stall_every_sec = min(max(settings.stall_every_sec, 0.0), 7200.0)
    return settings


# ---------------------------------------------------------------------------
# 语料消费：块级洗牌牌堆（块=8~25 行连续语料，空行分隔），抽尽才重洗
# ---------------------------------------------------------------------------
def _load_blocks(lang: str) -> list[list[str]]:
    path = os.path.join(CORPUS_DIR, f"{lang}.txt")
    if not os.path.isfile(path):
        raise SystemExit(
            f"语料文件缺失：{path}（先运行 py -3.10 -m uptime.tail._corpus_gen 生成）"
        )
    with open(path, encoding="utf-8") as f:
        text = f.read()
    blocks = [b.splitlines() for b in text.split("\n\n") if b.strip()]
    return [b for b in blocks if 8 <= len(b) <= 25]


class _Shoe:
    """洗牌牌堆：抽尽才重洗，重洗后首张不与上一张相同（防相邻重复）。"""

    def __init__(self, items: list):
        self._items = items
        self._last = None
        self._deck: list = []
        self._refill()

    def _refill(self) -> None:
        self._deck = list(range(len(self._items)))
        random.shuffle(self._deck)
        if (
            self._last is not None
            and len(self._deck) > 1
            and self._deck[0] == self._last
        ):
            self._deck[0], self._deck[1] = self._deck[1], self._deck[0]

    def next(self):
        if not self._deck:
            self._refill()
        idx = self._deck.pop()
        self._last = idx
        return self._items[idx]


# ---------------------------------------------------------------------------
# 日志/测试行生成：数字与时间戳全部运行时注入
# ---------------------------------------------------------------------------
_SERVICES = (
    "order_service", "inventory_service", "pricing_engine", "shipping_planner",
    "ledger_writer", "notify_dispatcher", "catalog_service", "reserve_manager",
)
_OPS = ("reserve", "commit", "rollback", "sync", "refresh", "apply", "dispatch", "resync")
_QUEUES = (
    "events.orders", "events.shipments", "dlq.payments", "tasks.invoices",
    "sync.inventory", "notify.emails",
)
_PKGS = (
    "core/rpc", "internal/queue", "pkg/repo", "services/billing",
    "libs/cache", "workers/poller", "adapters/db", "tools/migrate",
)
_QUERIES = (
    "select_orders_by_tenant", "upsert_shipment_row", "count_pending_refunds",
    "insert_invoice_lines", "update_cursor_revision", "join_sku_availability",
)
_TESTS = (
    "respects_idempotency", "rejects_stale_revision", "applies_deltas_in_order",
    "handles_broker_backpressure", "falls_back_to_snapshot", "drains_dead_letters",
    "rounds_window_percentiles", "circuit_reopens_after_cooldown",
)
_MODULES = (
    "order_flow", "retry_policy", "window_stats", "cursor_store", "dead_letter",
    "snapshot_loader", "pool_guard", "batch_writer", "lease_manager", "schema_registry",
)


class _LogFactory:
    """运行时日志行与测试输出行；内含单调推进的构建进度计数。"""

    def __init__(self) -> None:
        self._total = random.randint(320, 480)
        self._done = random.randint(0, self._total // 10)
        self._seq = random.randint(10000, 90000)

    # -- 进度 ------------------------------------------------------------
    def _progress(self) -> tuple[int, int]:
        self._done = min(self._done + random.randint(1, 9), self._total)
        if self._done >= self._total:
            self._total = random.randint(320, 480)
            self._done = random.randint(0, 24)
        return self._done, self._total

    # -- 时间戳日志行 ----------------------------------------------------
    def runtime_log(self) -> tuple[str, str, str]:
        """返回 (时间戳, 级别, 消息)。"""
        level = random.choice(_LEVELS)
        done, total = self._progress()
        self._seq += random.randint(1, 3)
        templates = (
            lambda: f"{random.choice(_SERVICES)}: rpc {random.choice(_OPS)} completed"
                    f" in {random.randint(12, 900)}ms (attempts={random.randint(1, 4)})",
            lambda: f"retry {random.randint(2, 6)}/{random.randint(6, 8)} for"
                    f" {random.choice(_SERVICES)}.{random.choice(_OPS)}"
                    f" after {random.randint(40, 1800)}ms backoff",
            lambda: f"cache hit rate {random.uniform(71.0, 99.8):.1f}% over"
                    f" {random.randint(30, 300)}s window"
                    f" (ttl={random.choice([30, 60, 120, 300, 600])}s,"
                    f" keys={random.randint(1200, 98000)})",
            lambda: f"db pool in_use={random.randint(1, 30)}/{random.choice([32, 48, 64])}"
                    f" waiters={random.randint(0, 6)} acquire {random.uniform(0.2, 9.0):.1f}ms",
            lambda: f"slow query {random.choice(_QUERIES)} took {random.randint(180, 940)}ms"
                    f" rows={random.randint(40, 5200)} (budget {random.choice([100, 150, 200, 300])}ms)",
            lambda: f"backpressure: {random.choice(_QUEUES)} depth={random.randint(800, 42000)}"
                    f" over soft limit {random.choice([5000, 10000, 20000])},"
                    f" shedding {random.choice([5, 10, 15, 20])}%",
            lambda: f"build progress {done}/{total} ({done * 100 // total}%)"
                    f" - {random.choice(_PKGS)}",
            lambda: f"compiled {random.choice(_PKGS)} in {random.randint(60, 2600)}ms"
                    f" ({random.randint(3, 86)} sources)",
            lambda: (lambda heap: f"gc pause {random.uniform(1.2, 38.0):.1f}ms"
                     f" heap={heap}MB live={random.randint(120, max(heap - 50, 130))}MB")(
                random.randint(220, 2400)),
            lambda: f"flushed {random.randint(64, 4096)} events to {random.choice(_QUEUES)}"
                    f" in {random.randint(8, 240)}ms, lag={random.randint(120, 9000)}ms",
            lambda: f"heartbeat seq={self._seq} rtt={random.uniform(0.4, 60.0):.1f}ms"
                    f" peers={random.randint(2, 9)}",
            lambda: f"dedupe skip event id={random.randint(10**6, 10**8)}"
                    f" (idempotency key {random.randint(10**9, 10**10)})",
            lambda: f"circuit breaker {random.choice(['half-open', 're-opened'])}"
                    f" on {random.choice(_SERVICES)}"
                    f" ({random.randint(3, 21)} failures/{random.choice([30, 60, 120])}s window)",
            lambda: f"cache warmup {random.choice(_PKGS)}: {random.randint(500, 18000)} keys"
                    f" loaded in {random.randint(90, 3200)}ms",
            lambda: f"connection #{random.randint(40, 3800)} recycled after"
                    f" {random.randint(60, 1800)}s ({random.randint(900, 92000)} requests served)",
            lambda: f"shadow traffic {random.choice([5, 10, 20])}% ->"
                    f" {random.choice(_SERVICES)}.{random.choice(_OPS)}"
                    f" mismatches={random.randint(0, 12)}/{random.randint(1000, 50000)}",
        )
        now = get_now()
        ts = now.strftime("%H:%M:%S") + f".{now.microsecond // 1000:03d}"
        return ts, level, random.choice(templates)()

    # -- 测试输出行（不带时间戳，跟语言风格） ------------------------------
    def test_line(self, lang: str) -> str:
        name = random.choice(_TESTS)
        cls = "".join(p.title() for p in name.split("_"))
        secs = random.uniform(0.01, 1.8)
        if lang == "python":
            return random.choice((
                f"PASS tests/test_{random.choice(_MODULES)}.py"
                f"::test_{name} ({secs:.2f}s)",
                f"ok {random.randint(3, 240)} - test_{name}",
                f"----- coverage: {random.uniform(61.0, 93.0):.1f}% -----",
            ))
        if lang == "js":
            return random.choice((
                f"PASS {random.choice(_PKGS)}/{random.choice(_MODULES)}.spec.js"
                f" ({random.randint(2, 24)} tests, {secs:.2f}s)",
                f"  ✓ {name} ({secs:.2f}s)",
                f"Tests: {random.randint(24, 320)} passed,"
                f" {random.randint(0, 9)} skipped, {random.uniform(1.2, 26.0):.1f}s",
            ))
        return random.choice((
            f"=== RUN   Test{cls}",
            f"--- PASS: Test{cls} ({secs:.2f}s)",
            f"ok  \t{random.choice(_PKGS)}\t{secs:.2f}s",
        ))


# ---------------------------------------------------------------------------
# 着色（克制：注释淡绿、级别按色、测试 PASS 淡绿）
# ---------------------------------------------------------------------------
def _color_code_line(prefix: str, code: str) -> str:
    """代码行着色：file:line: 前置串淡显，注释淡绿；非 TTY 输出原文。"""
    if not _USE_COLOR:
        return prefix + code
    head = _C_DIM + prefix + _C_RESET if prefix else ""
    for marker in (" # ", " // "):
        idx = code.find(marker)
        if idx >= 0:
            cut = idx + 1
            return head + code[:cut] + _C_GREEN + _C_DIM + code[cut:] + _C_RESET
    if code.startswith("#") or code.startswith("//"):
        return head + _C_GREEN + _C_DIM + code + _C_RESET
    return head + code


# 块 → 伪文件路径：从块首的 def/class/func 提取名字（file:line: 前缀用）
_DEF_PATTERNS = {
    "python": re.compile(r"\bdef (\w+)|\bclass (\w+)"),
    "js": re.compile(r"\bfunction (\w+)|\bclass (\w+)|describe\('([^']+)'"),
    "go": re.compile(r"\bfunc (?:\([^)]*\) )?(\w+)|\btype (\w+)"),
}


def _snake(name: str) -> str:
    out = re.sub(r"(?<!^)([A-Z])", r"_\1", name.replace(".", "_")).lower()
    return out


def _block_path(lang: str, block: list[str]) -> str:
    """给代码块起一个可信的伪文件路径（pkg/名字.ext）。"""
    name = None
    pat = _DEF_PATTERNS[lang]
    for line in block[:8]:
        m = pat.search(line)
        if m:
            name = next(g for g in m.groups() if g)
            break
    pkg = random.choice(_PKGS)
    if name is None:
        name = random.choice(("handler", "worker", "client", "store", "helpers"))
    if lang == "python":
        return f"{pkg}/{name}.py"
    if lang == "js":
        ext = ".spec.js" if name.startswith(("test", "describe")) or "spec" in name else ".js"
        return f"{pkg}/{_snake(name)}{ext}"
    ext = "_test.go" if name.startswith("Test") else ".go"
    return f"{pkg}/{_snake(name)}{ext}"


def _color_log_line(ts: str, level: str, msg: str) -> str:
    if not _USE_COLOR:
        return f"{ts} [{level}] {msg}"
    return (
        f"{_C_DIM}{ts}{_C_RESET} {_LEVEL_COLOR[level]}[{level}]{_C_RESET} {msg}"
    )


def _color_test_line(line: str) -> str:
    if not _USE_COLOR:
        return line
    if "PASS" in line or "✓" in line or line.startswith("ok"):
        return _C_GREEN + line + _C_RESET
    return _C_DIM + line + _C_RESET


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
class _Engine:
    """滚动输出引擎：段落调度 + 节奏/卡顿/爆发 + 自动退出。"""

    def __init__(self, settings: TailSettings) -> None:
        self.settings = settings
        self.base_delay = 1.0 / settings.lines_per_sec
        self.deadline = (
            time.monotonic() + auto_exit_seconds()
            if auto_exit_seconds() is not None
            else None
        )
        self.rotation = _Shoe(list(LANGS))
        self.decks = {lang: _Shoe(_load_blocks(lang)) for lang in LANGS}
        self.logs = _LogFactory()
        self.recent: deque[str] = deque(maxlen=8)
        self.burst_left = 0
        self.next_stall_at = self._schedule_stall(time.monotonic())
        self.out = sys.stdout

    # -- 时间 ------------------------------------------------------------
    def _nap(self, delay: float) -> None:
        """睡 delay 秒，但不越过退出期限；到点抛 _TimeUp。"""
        if self.deadline is None:
            time.sleep(delay)
            return
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _TimeUp
        time.sleep(min(delay, remaining))
        if time.monotonic() >= self.deadline:
            raise _TimeUp

    # -- 卡顿 ------------------------------------------------------------
    def _schedule_stall(self, now: float) -> float:
        if self.settings.stall_every_sec <= 0:
            return float("inf")
        every = self.settings.stall_every_sec
        spread = self.settings.stall_spread
        return now + random.uniform(every * (1 - spread), every * (1 + spread))

    def _maybe_stall(self) -> None:
        now = time.monotonic()
        if now < self.next_stall_at:
            return
        self._nap(random.uniform(*STALL_DURATION_RANGE))
        self.next_stall_at = self._schedule_stall(time.monotonic())
        # 恢复后来一小波快刷
        self.burst_left = random.randint(*POST_STALL_BURST_RANGE)

    # -- 节奏 ------------------------------------------------------------
    def _next_delay(self) -> float:
        if self.burst_left > 0:
            self.burst_left -= 1
            return self.base_delay * BURST_FACTOR
        if random.random() < BURST_PROB:
            self.burst_left = random.randint(*BURST_LEN_RANGE) - 1
            return self.base_delay * BURST_FACTOR
        return self.base_delay * random.uniform(*DELAY_JITTER)

    # -- 输出 ------------------------------------------------------------
    def _emit(self, line: str, plain: str) -> None:
        """写一行并立即 flush（管道模式保持流式）；recent 记明文用于判重。"""
        self.out.write(line + "\n")
        self.out.flush()
        self.recent.append(plain)

    def _dynamic_line(self, lang: str) -> tuple[str, str]:
        """生成一行日志/测试输出，返回 (展示行, 明文行)。

        明文行撞上最近输出则重造（最多 8 次），保证连续窗口不出现完全重复。
        """
        candidate = ("", "")
        for _ in range(8):
            if random.random() < TEST_LINE_PROB:
                plain = self.logs.test_line(lang)
                candidate = (_color_test_line(plain), plain)
            else:
                ts, level, msg = self.logs.runtime_log()
                plain = f"{ts} [{level}] {msg}"
                candidate = (_color_log_line(ts, level, msg), plain)
            if candidate[1] not in self.recent:
                break
        return candidate

    # -- 主循环 ------------------------------------------------------------
    def run(self) -> None:
        try:
            while True:
                lang = self.rotation.next()
                block = self.decks[lang].next()
                # file:line: 前缀让代码行天然不重复（grep/lint 式构建输出观感）
                path = _block_path(lang, block)
                start = random.randint(40, 2600)
                for i, raw in enumerate(block):
                    prefix = f"{path}:{start + i}: "
                    self._maybe_stall()
                    self._emit(_color_code_line(prefix, raw), prefix + raw)
                    self._nap(self._next_delay())
                for _ in range(random.randint(*LOG_COUNT_PER_SEGMENT)):
                    self._maybe_stall()
                    line, plain = self._dynamic_line(lang)
                    self._emit(line, plain)
                    self._nap(self._next_delay())
        except _TimeUp:
            pass


def main() -> None:
    cfg = load_config()
    settings = resolve_settings(cfg)
    # 标题序列只写真实终端；管道/重定向下保持输出纯净
    if _USE_COLOR:
        set_console_title(WINDOW_TITLE)
    try:
        _Engine(settings).run()
    except KeyboardInterrupt:
        pass
