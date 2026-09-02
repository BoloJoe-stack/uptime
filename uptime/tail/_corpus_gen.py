"""tail 语料生成脚本（一次性工具，产出后运行无需再跑）。

生成 data/code_corpus/{python,js,go}.txt 三个语料文件，供 uptime.tail 运行时消费：
- 每个文件 400~600 行（含块间空行），内容为虚构但逼真的后端代码
- 以"块"为单位：每块 8~25 行、块内无空行、块间恰好一个空行
- 运行时按块洗牌消费，因此块必须语义自洽（一个完整函数/类/测试）
- 纯 ASCII；不含密钥样式赋值、真实域名/公司名

用法：在仓库根执行 py -3.10 -m uptime.tail._corpus_gen
固定随机种子，可重复生成相同语料。
"""

from __future__ import annotations

import random
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = PROJECT_ROOT / "data" / "code_corpus"

# 固定种子：语料可复现
_SEED = 20260902

# ---------------------------------------------------------------------------
# 词表：全部为通用业务词，虚构组合
# ---------------------------------------------------------------------------
ENTITIES = [
    "order", "shipment", "invoice", "sku", "payment", "refund",
    "basket", "coupon", "ledger_entry", "carrier_slot",
]
SERVICES = [
    "order_service", "inventory_service", "pricing_engine", "shipping_planner",
    "ledger_writer", "notify_dispatcher", "catalog_service", "reserve_manager",
]
OPS = ["reserve", "commit", "rollback", "sync", "refresh", "apply", "dispatch", "resync"]
QUEUE_NAMES = [
    "events.orders", "events.shipments", "dlq.payments", "tasks.invoices",
    "sync.inventory", "notify.emails",
]
PKG_PATHS = [
    "core/rpc", "internal/queue", "pkg/repo", "services/billing",
    "libs/cache", "workers/poller", "adapters/db", "tools/migrate",
]

# 违禁串只查技术类样式；中文等非 ASCII 词由"语料纯 ASCII"断言一并覆盖
FORBIDDEN = [
    "password", "passwd", "api_key", "apikey", "secret", "token",
]
# 域名样式：.com/.cn/... 后接词边界（排除 .commit/.connect 这类代码标识符）
_DOMAIN_RE = re.compile(r"\.(com|cn|net|org|io|dev|app)\b", re.IGNORECASE)


_TOKEN_RE = re.compile(r"@(\w+)@?")


def _sub(t: str, m: dict[str, object]) -> list[str]:
    """把 @token@（结尾 @ 可省略）替换成参数值并切成行。

    只替换词表里存在的 token，因此 Python 装饰器（@router. / @functools.
    / @classmethod）不受影响。块内空行丢弃：空行留作块间分隔符。
    """
    def repl(mo: re.Match) -> str:
        name = mo.group(1)
        return str(m[name]) if name in m else mo.group(0)

    t = _TOKEN_RE.sub(repl, t)
    lines = []
    for ln in t.splitlines():
        if not ln.strip():
            continue
        # 残留的 @ 只允许出现在行首（装饰器），否则视为漏替换
        stripped = ln.lstrip()
        if "@" in stripped[1:]:
            raise AssertionError(f"疑似未替换 token: {ln}")
        lines.append(ln)
    return lines


def _camel(s: str) -> str:
    return "".join(p.title() for p in s.split("_"))


def _params(r: random.Random) -> dict[str, object]:
    e = r.choice(ENTITIES)
    return {
        "e": e,
        "E": _camel(e),
        "svc": r.choice(SERVICES),
        "Svc": _camel(r.choice(SERVICES)),
        "op": r.choice(OPS),
        "Op": _camel(r.choice(OPS)),
        "q": r.choice(QUEUE_NAMES),
        "pkg": r.choice(PKG_PATHS),
        "n2": r.randint(10, 99),
        "n3": r.randint(100, 999),
        "n4": r.randint(3, 6),
        "batch": r.choice([32, 64, 128, 256, 512]),
        "ttl": r.choice([30, 60, 120, 300, 600, 900]),
        "timeout": r.choice([250, 500, 1000, 2000, 5000]),
        "budget": r.choice([50, 100, 150, 200, 300]),
        "base": r.choice(["0.05", "0.1", "0.2", "0.25"]),
        "factor": r.choice(["1.8", "2.0", "2.5"]),
    }


# ---------------------------------------------------------------------------
# Python 块模板
# ---------------------------------------------------------------------------
PY_TEMPLATES = [
    lambda r: _sub('''def load_@e@_snapshot(@e@_id, session=None, *, bypass_cache=False):
    """Cache-aside loader for @E@ rows."""
    key = f"@svc@:@e@:{@e@_id}"
    if not bypass_cache:
        cached = cache_client.get(key)
        if cached is not None:
            metrics.incr("@e@.cache_hit")
            return @E@Snapshot.from_dict(cached)
    row = session.execute(
        select(@E@Row).where(@E@Row.id == @e@_id)
    ).scalar_one_or_none()
    if row is None:
        raise @E@NotFound(f"@e@ {@e@_id} not found")
    snapshot = @E@Snapshot.from_row(row)
    cache_client.setex(key, CACHE_TTL, snapshot.to_dict())
    metrics.incr("@e@.cache_miss")
    return snapshot''', _params(r)),

    lambda r: _sub('''def with_retries(fn=None, *, attempts=@n4, base_delay=@base):
    """Retry transient failures with exponential backoff and jitter."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            delay = base_delay
            for attempt in range(1, attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except TransientError as exc:
                    if attempt == attempts:
                        raise RetryExhausted(f"failed after {attempts} attempts") from exc
                    metrics.observe("retry.backoff_ms", delay * 1000)
                    time.sleep(delay * (1 + random.uniform(0, 0.1)))
                    delay = min(delay * @factor@, MAX_BACKOFF_S)
        return wrapper
    return deco if fn is None else deco(fn)''', _params(r)),

    lambda r: _sub('''@router.post("/internal/v1/@e@s/@e@_id/resend")
async def resend_@e@(request: Request, @e@_id: int):
    span = request.state.tracer.start_span("resend.@e@")
    try:
        cmd = ResendCommand(
            @e@_id=@e@_id,
            reason=request.query_params.get("reason", "manual"),
            idempotency_key=request.headers.get("x-idempotency-key", ""),
        )
        if not cmd.idempotency_key:
            raise ValueError("idempotency key is required")
        result = await @svc@.resend(cmd)
        span.set_tag("resend.accepted", result.accepted)
        return {"status": "accepted", "sequence": result.sequence}
    except ValidationError as exc:
        span.set_tag("resend.invalid", True)
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    finally:
        span.finish()''', _params(r)),

    lambda r: _sub('''class @E@Worker:
    """Consumes @e@ events from the queue and applies side effects."""

    def __init__(self, queue, sink, *, batch_size=@batch):
        self._queue = queue
        self._sink = sink
        self._batch_size = batch_size
        self._handled = 0

    async def run(self):
        while True:
            batch = await self._queue.collect(self._batch_size, timeout=@timeout)
            if not batch:
                metrics.observe("queue.depth", await self._queue.depth())
                continue
            async with self._sink.transaction():
                for event in batch:
                    await self._sink.apply(event)
            await self._queue.ack_all(batch)
            self._handled += len(batch)
            metrics.observe("worker.handled", self._handled)''', _params(r)),

    lambda r: _sub('''class BucketLimiter:
    """Small bucket-based limiter used to shield the @svc@ from burst traffic."""

    def __init__(self, rate_per_s: float, capacity: int = @n3@):
        self.rate = rate_per_s
        self.capacity = capacity
        self._level = float(capacity)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def try_acquire(self, n: int = 1) -> bool:
        with self._lock:
            now = time.monotonic()
            self._level = min(
                self.capacity, self._level + (now - self._updated) * self.rate
            )
            self._updated = now
            if self._level >= n:
                self._level -= n
                return True
            metrics.incr("limiter.rejected")
            return False''', _params(r)),

    lambda r: _sub('''def aggregate_window(rows: list[Sample], window_s: int = @n2) -> WindowStats:
    """Fold raw samples into a fixed window summary."""
    if not rows:
        return WindowStats.empty(window_s)
    cutoff = rows[-1].ts - timedelta(seconds=window_s)
    recent = [r for r in rows if r.ts >= cutoff]
    latencies = sorted(r.latency_ms for r in recent)
    return WindowStats(
        count=len(recent),
        p50=percentile(latencies, 0.50),
        p95=percentile(latencies, 0.95),
        p99=percentile(latencies, 0.99),
        error_rate=sum(1 for r in recent if r.failed) / len(recent),
        window_s=window_s,
    )''', _params(r)),

    lambda r: _sub('''def load_settings(profile: str = "default") -> Settings:
    """Layered config: defaults -> file -> env overrides."""
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    section = (raw.get(profile) or {})
    settings = Settings(
        dsn=section.get("dsn", DEFAULT_DSN),
        pool_size=int(section.get("pool_size", @n2)),
        rpc_timeout_ms=int(section.get("rpc_timeout_ms", @timeout)),
        cache_ttl_s=int(section.get("cache_ttl_s", @ttl)),
    )
    if os.environ.get("APP_ENV") == "canary":
        settings.pool_size = max(4, settings.pool_size // 2)
    if settings.pool_size < 4:
        raise ValueError(f"pool_size too small: {settings.pool_size}")
    return settings''', _params(r)),

    lambda r: _sub('''def test_@op@_@e@_is_idempotent(harness):
    @e@_id = harness.seed_@e@(state="pending")
    first = harness.svc.@op@(cmd(@e@_id=@e@_id))
    second = harness.svc.@op@(cmd(@e@_id=@e@_id))
    assert first.sequence == second.sequence
    assert harness.count_@e@_rows(@e@_id) == 1
    harness.assert_metric("@svc@.@op@.calls", 2)
    harness.assert_no_dead_letters()


def test_@op@_@e@_rejects_stale_payload(harness):
    @e@_id = harness.seed_@e@(state="pending", version=@n2)
    stale = cmd(@e@_id=@e@_id, expected_version=@n2 - 1)
    with pytest.raises(StaleVersionError):
        harness.svc.@op@(stale)
    harness.assert_metric("@svc@.@op@.stale_rejected", 1)''', _params(r)),

    lambda r: _sub('''class @Svc@Client:
    """Thin async wrapper around the @svc@ RPC surface."""

    def __init__(self, channel, *, timeout_ms: int = @timeout):
        self._stub = @Svc@Stub(channel)
        self._timeout_ms = timeout_ms
        self._inflight = 0

    async def @op@(self, request: @Op@Request) -> @Op@Reply:
        self._inflight += 1
        metrics.gauge("@svc@.inflight", self._inflight)
        try:
            return await self._stub.@Op@(
                request, timeout=self._timeout_ms / 1000.0
            )
        except RpcError as exc:
            if exc.code() in RETRYABLE_CODES:
                raise TransientError(str(exc)) from exc
            raise
        finally:
            self._inflight -= 1
            metrics.gauge("@svc@.inflight", self._inflight)''', _params(r)),

    lambda r: _sub('''async def publish_@e@_events(events, *, topic: str = "@q@"):
    """Batch-publish @e@ domain events, retrying on broker backpressure."""
    for chunk in chunks(events, @batch):
        payload = [e.as_avro() for e in chunk]
        for attempt in range(1, @n4 + 1):
            try:
                await producer.send_batch(topic, payload)
                break
            except BrokerAtCapacity:
                if attempt == @n4:
                    dead_letters.extend(chunk)
                    metrics.incr("@q@.dead_letter")
                    continue
                backoff = @base@ * (@factor@ ** (attempt - 1))
                await asyncio.sleep(backoff)
    return len(events) - len(dead_letters)''', _params(r)),

    lambda r: _sub('''class @E@Serializer:
    """Wire format for @e@ payloads, tolerant of legacy field names."""

    FIELD_MAP = {"created": "created_at", "ref": "reference_id"}

    @classmethod
    def to_wire(cls, snapshot: @E@Snapshot) -> dict:
        data = asdict(snapshot)
        for old, new in cls.FIELD_MAP.items():
            if old in data:
                data[new] = data.pop(old)
        data["schema_version"] = SCHEMA_VERSION
        return data

    @classmethod
    def from_wire(cls, payload: dict) -> @E@Snapshot:
        version = payload.get("schema_version", 0)
        if version > SCHEMA_VERSION:
            raise SchemaMismatch(f"got {version}, support <= {SCHEMA_VERSION}")
        if version < SCHEMA_VERSION:
            payload = migrate(payload, from_version=version)
        return @E@Snapshot(**payload)''', _params(r)),

    lambda r: _sub('''def backfill_@e@_cursors(conn, *, dry_run: bool = True) -> int:
    """One-shot migration: rebuild @e@ cursors from the audit table."""
    updated = 0
    with conn.transaction():
        rows = conn.execute(
            "SELECT @e@_id, MAX(revision) FROM @e@_audit "
            "WHERE repaired_at IS NULL GROUP BY @e@_id"
        )
        for @e@_id, revision in rows:
            if dry_run:
                logger.info("would repair %s -> r%s", @e@_id, revision)
                continue
            conn.execute(
                "UPDATE @e@_cursor SET revision = %s, repaired_at = now() "
                "WHERE @e@_id = %s",
                (revision, @e@_id),
            )
            updated += 1
    return updated''', _params(r)),
]

# ---------------------------------------------------------------------------
# JS 块模板
# ---------------------------------------------------------------------------
JS_TEMPLATES = [
    lambda r: _sub('''router.post('/internal/v1/@e@s/:id/resend', async (req, res) => {
  const cmd = {
    @e@Id: Number(req.params.id),
    reason: req.query.reason ?? 'manual',
    idempotencyKey: req.headers['x-idempotency-key'] ?? null,
  };
  if (!cmd.idempotencyKey) {
    return res.status(400).json({ error: 'missing idempotency key' });
  }
  try {
    const result = await @svc@.resend(cmd);
    metrics.incr('@svc@.resend.accepted');
    res.json({ status: 'accepted', sequence: result.sequence });
  } catch (err) {
    if (err instanceof ValidationError) {
      return res.status(422).json({ error: 'invalid payload', details: err.fields });
    }
    req.log.warn({ err, cmd }, 'resend @e@ failed');
    res.status(502).json({ error: 'upstream unavailable' });
  }
});''', _params(r)),

    lambda r: _sub('''async function withRetries(fn, opts = {}) {
  const { attempts = @n4, baseDelayMs = @n2, factor = @factor@ } = opts;
  let delayMs = baseDelayMs;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      return await fn(attempt);
    } catch (err) {
      if (attempt === attempts || !isRetryable(err)) {
        throw new RetryExhausted(`failed after ${attempt} attempts`, { cause: err });
      }
      metrics.observe('retry.backoff_ms', delayMs);
      await sleep(delayMs + Math.random() * delayMs * 0.1);
      delayMs = Math.min(delayMs * factor, MAX_BACKOFF_MS);
    }
  }
  throw new Error('unreachable');
}''', _params(r)),

    lambda r: _sub('''class TTLCache {
  constructor({ maxEntries = @n3, ttlMs = @ttl@000 } = {}) {
    this.maxEntries = maxEntries;
    this.ttlMs = ttlMs;
    this.map = new Map();
  }
  get(key) {
    const hit = this.map.get(key);
    if (!hit) return undefined;
    if (Date.now() - hit.storedAt > this.ttlMs) {
      this.map.delete(key);
      metrics.incr('cache.expired');
      return undefined;
    }
    this.map.delete(key);
    this.map.set(key, hit);
    return hit.value;
  }
  set(key, value) {
    if (this.map.size >= this.maxEntries) {
      this.map.delete(this.map.keys().next().value);
    }
    this.map.set(key, { value, storedAt: Date.now() });
  }
}''', _params(r)),

    lambda r: _sub('''async function drain@E@Batch(consumer, sink, { maxBatch = @batch, timeoutMs = @timeout } = {}) {
  const batch = await consumer.take({ limit: maxBatch, timeoutMs });
  if (batch.length === 0) {
    metrics.gauge('@q@.depth', await consumer.depth());
    return 0;
  }
  const span = tracer.startSpan('drain.@e@');
  try {
    await sink.transaction(async (tx) => {
      for (const event of batch) {
        await tx.apply(event);
      }
    });
    await consumer.ackAll(batch);
    span.setTag('batch.size', batch.length);
    return batch.length;
  } catch (err) {
    span.setTag('batch.failed', true);
    await consumer.nackAll(batch, { retryInMs: backoffFor(err) });
    throw err;
  } finally {
    span.finish();
  }
}''', _params(r)),

    lambda r: _sub('''async function withTransaction(pool, fn) {
  const client = await pool.connect();
  const q = client.query.bind(client);
  try {
    await q('BEGIN');
    const result = await fn(q);
    await q('COMMIT');
    return result;
  } catch (err) {
    await q('ROLLBACK').catch(() => {});
    if (isSerializationFailure(err)) {
      metrics.incr('db.serialization_retry');
      return withTransaction(pool, fn);
    }
    throw err;
  } finally {
    client.release();
  }
}''', _params(r)),

    lambda r: _sub('''describe('@svc@.@op@', () => {
  it('applies deltas in arrival order', async () => {
    const deltas = makeDeltas(@n2);
    const applied = [];
    await applyDeltas(deltas, { sink: recorder(applied) });
    expect(applied.map((d) => d.seq)).toEqual(deltas.map((d) => d.seq));
  });
  it('stops on first non-retryable error', async () => {
    const sink = failingSink(new RangeError('bad revision'));
    await expect(applyDeltas(makeDeltas(3), { sink })).rejects.toThrow('bad revision');
    expect(sink.calls).toBe(1);
  });
  it('keeps idempotency across replays', async () => {
    const events = makeEvents(2);
    await dispatch(events);
    await dispatch(events);
    expect(store.size).toBe(events.length);
  });
});''', _params(r)),

    lambda r: _sub('''function attachTracing(app) {
  app.use((req, res, next) => {
    const parent = headerToContext(req.headers['x-trace-parent']);
    const span = tracer.startSpan(`http ${req.method}`, { childOf: parent });
    span.setTag('http.path', req.path);
    req.log = logger.child({ traceId: span.traceId, path: req.path });
    res.on('finish', () => {
      span.setTag('http.status', res.statusCode);
      if (res.statusCode >= 500) {
        metrics.incr('http.server_error');
      }
      span.finish();
    });
    next();
  });
  return app;
}''', _params(r)),

    lambda r: _sub('''class @E@Queue {
  constructor(redis, { name = '@q@', maxInflight = @n2 } = {}) {
    this.redis = redis;
    this.name = name;
    this.maxInflight = maxInflight;
    this.inflight = new Map();
  }
  async enqueue(job) {
    const id = makeJobId('@e@');
    await this.redis.hset(keys.jobs(this.name), id, encode(job));
    await this.redis.zadd(keys.pending(this.name), nowTs(), id);
    metrics.incr('@q@.enqueued');
    return id;
  }
  async claim() {
    const ids = await this.redis.zpopmin(keys.pending(this.name), 1);
    if (ids.length === 0) return null;
    const [id, score] = ids;
    await this.redis.zadd(keys.inflight(this.name), score + VISIBILITY_S, id);
    return decode(await this.redis.hget(keys.jobs(this.name), id));
  }
}''', _params(r)),

    lambda r: _sub('''function make@Svc@Client({ baseUrl, timeoutMs = @timeout } = {}) {
  const client = baseClient({ baseUrl, timeoutMs, retries: @n4 });
  client.interceptors.request.use((config) => {
    config.headers['x-request-id'] = makeRequestId();
    config.headers['x-trace-parent'] = currentTraceParent();
    return config;
  });
  client.interceptors.response.use(
    (res) => {
      metrics.observe('@svc@.latency_ms', res.durationMs);
      return res;
    },
    (err) => {
      if (err.response && err.response.status === @n3) {
        metrics.incr('@svc@.circuit_open');
        circuit.trip('@svc@');
      }
      return Promise.reject(err);
    },
  );
  return client;
}''', _params(r)),

    lambda r: _sub('''function validate@E@Payload(payload) {
  const errors = [];
  if (!Number.isInteger(payload.@e@Id) || payload.@e@Id <= 0) {
    errors.push({ field: '@e@Id', rule: 'positive_int' });
  }
  if (!ALLOWED_STATES.has(payload.state)) {
    errors.push({ field: 'state', rule: 'enum', got: payload.state });
  }
  if (payload.reference && payload.reference.length > 64) {
    errors.push({ field: 'reference', rule: 'max_len_64' });
  }
  if (payload.lines && payload.lines.length === 0) {
    errors.push({ field: 'lines', rule: 'non_empty' });
  }
  if (errors.length > 0) {
    throw new ValidationError('@e@ payload rejected', errors);
  }
  return normalize(payload);
}''', _params(r)),

    lambda r: _sub('''async function run@Op@Scheduler({ intervalMs = @n2@00, jitterMs = @n2@0 } = {}) {
  let running = true;
  while (running) {
    const started = Date.now();
    try {
      const lease = await acquireLease('@op@.@e@', { ttlMs: @ttl@000 });
      if (lease) {
        const cursor = await loadCursor('@e@.@op@');
        const pushed = await pushPending(cursor, { limit: @batch });
        metrics.observe('@op@.pushed', pushed);
        await lease.release();
      }
    } catch (err) {
      logger.warn({ err }, '@op@ @e@ tick failed');
      metrics.incr('@op@.tick_error');
    }
    const elapsed = Date.now() - started;
    await sleep(Math.max(intervalMs - elapsed, 0) + Math.random() * jitterMs);
  }
}''', _params(r)),

    lambda r: _sub('''async function probe@Svc@Health(client, { intervalMs = @n2@00 } = {}) {
  const states = [];
  for (;;) {
    const t0 = process.hrtime.bigint();
    try {
      const res = await client.ping({ timeoutMs: @timeout });
      const rttMs = Number(process.hrtime.bigint() - t0) / 1e6;
      states.push({ ok: res.status === 'ok', rttMs });
      if (states.length > @n2) states.shift();
      metrics.observe('@svc@.probe.rtt_ms', rttMs);
    } catch (err) {
      states.push({ ok: false, rttMs: -1 });
      metrics.incr('@svc@.probe.failed');
    }
    const okRate = states.filter((s) => s.ok).length / states.length;
    reportHealth('@svc@', okRate >= 0.6 ? 'healthy' : 'degraded');
    await sleep(intervalMs);
  }
}''', _params(r)),
]

# ---------------------------------------------------------------------------
# Go 块模板（4 空格缩进书写，生成时统一转 tab）
# ---------------------------------------------------------------------------
GO_TEMPLATES = [
    lambda r: _sub('''func (s *Server) handle@Op@@E@(w http.ResponseWriter, r *http.Request) {
    ctx, cancel := context.WithTimeout(r.Context(), s.rpcTimeout)
    defer cancel()
    id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
    if err != nil {
        render.Error(w, http.StatusBadRequest, "invalid @e@ id")
        return
    }
    summary, err := s.client.@Op@(ctx, &@Op@Request{Id: id, Reason: "manual"})
    if err != nil {
        s.log.Warn("@op@ @e@ failed", "id", id, "err", err)
        render.Error(w, http.StatusBadGateway, "upstream unavailable")
        return
    }
    s.metrics.Incr("@svc@.@op@.accepted")
    render.JSON(w, http.StatusOK, summary)
}''', _params(r)),

    lambda r: _sub('''func (c *@Svc@Client) @Op@@E@(ctx context.Context, req *@Op@Request) (*@Op@Reply, error) {
    var lastErr error
    backoff := c.initialBackoff
    for attempt := 1; attempt <= c.maxAttempts; attempt++ {
        reply, err := c.rpc.@Op@@E@(ctx, req)
        if err == nil {
            return reply, nil
        }
        if !isRetryable(err) {
            return nil, fmt.Errorf("@op@ @e@: %w", err)
        }
        lastErr = err
        c.metrics.Observe("retry.backoff_ms", backoff.Milliseconds())
        if err := sleepCtx(ctx, backoff); err != nil {
            return nil, err
        }
        backoff = time.Duration(float64(backoff) * c.backoffFactor)
    }
    return nil, fmt.Errorf("@op@ @e@ exhausted retries: %w", lastErr)
}''', _params(r)),

    lambda r: _sub('''type @E@Cache struct {
    mu        sync.RWMutex
    entries   map[string]@E@Entry
    ttl       time.Duration
    maxKeys   int
}

func New@E@Cache(ttl time.Duration, maxKeys int) *@E@Cache {
    return &@E@Cache{
        entries: make(map[string]@E@Entry, maxKeys),
        ttl:     ttl,
        maxKeys: maxKeys,
    }
}

func (c *@E@Cache) Get(key string) (@E@Entry, bool) {
    c.mu.RLock()
    e, ok := c.entries[key]
    c.mu.RUnlock()
    if !ok || time.Since(e.storedAt) > c.ttl {
        cacheMisses.Inc()
        return @E@Entry{}, false
    }
    cacheHits.Inc()
    return e, true
}''', _params(r)),

    lambda r: _sub('''func Start@E@Workers(ctx context.Context, n int, in <-chan @E@Event, apply func(context.Context, @E@Event) error) *sync.WaitGroup {
    var wg sync.WaitGroup
    wg.Add(n)
    for i := 0; i < n; i++ {
        go func(worker int) {
            defer wg.Done()
            for {
                select {
                case <-ctx.Done():
                    return
                case ev, ok := <-in:
                    if !ok {
                        return
                    }
                    if err := apply(ctx, ev); err != nil {
                        log.Warn("apply @e@ failed", "worker", worker, "err", err)
                        deadLetter.Publish(ev, err)
                    }
                    applied.WithLabelValues(ev.Tenant).Inc()
                }
            }
        }(i)
    }
    return &wg
}''', _params(r)),

    lambda r: _sub('''func scan@E@Rows(rows *sql.Rows) ([]@E@Row, error) {
    defer rows.Close()
    out := make([]@E@Row, 0, @n3)
    for rows.Next() {
        var r @E@Row
        if err := rows.Scan(&r.ID, &r.Tenant, &r.State, &r.UpdatedAt, &r.Payload); err != nil {
            return nil, fmt.Errorf("scan @e@ row: %w", err)
        }
        if r.Payload == nil {
            r.Payload = []byte("{}")
        }
        out = append(out, r)
    }
    return out, rows.Err()
}''', _params(r)),

    lambda r: _sub('''type breakerState int32

const (
    breakerClosed breakerState = iota
    breakerHalfOpen
    breakerOpen
)

func (b *Breaker) Allow() bool {
    switch breakerState(atomic.LoadInt32(&b.state)) {
    case breakerOpen:
        if time.Since(b.openedAt) < b.cooldown {
            return false
        }
        if atomic.CompareAndSwapInt32(&b.state, int32(breakerOpen), int32(breakerHalfOpen)) {
            b.probes.Store(0)
        }
        return true
    case breakerHalfOpen:
        return b.probes.Add(1) <= b.maxProbes
    default:
        return true
    }
}''', _params(r)),

    lambda r: _sub('''func (g *@E@Group) Refresh(ctx context.Context) error {
    v, err, _ := g.sf.Do("@e@.refresh", func() (any, error) {
        rows, err := g.repo.Latest@E@s(ctx, g.limit)
        if err != nil {
            return nil, fmt.Errorf("load @e@s: %w", err)
        }
        snapshot := make(map[string]@E@Row, len(rows))
        for _, r := range rows {
            snapshot[r.Key] = r
        }
        g.mu.Lock()
        g.snapshot = snapshot
        g.loadedAt = time.Now()
        g.mu.Unlock()
        g.metrics.Gauge("@e@.snapshot_size", float64(len(snapshot)))
        return len(snapshot), nil
    })
    if err != nil {
        return err
    }
    g.log.Debug("refreshed @e@ snapshot", "rows", v.(int))
    return nil
}''', _params(r)),

    lambda r: _sub('''func Test@Op@@E@Idempotent(t *testing.T) {
    cases := []struct {
        name    string
        replay  int
        wantErr error
    }{
        {"first apply", 1, nil},
        {"replay twice", 2, nil},
        {"replay after eviction", 3, ErrCacheMiss},
    }
    for _, tc := range cases {
        t.Run(tc.name, func(t *testing.T) {
            h := newTestHarness(t)
            for i := 0; i < tc.replay; i++ {
                err := h.svc.@op@(h.ctx, h.req)
                if !errors.Is(err, tc.wantErr) {
                    t.Fatalf("@op@ @e@ replay %d: err=%v want=%v", i, err, tc.wantErr)
                }
            }
        })
    }
}''', _params(r)),

    lambda r: _sub('''type Server struct {
    log        *slog.Logger
    metrics    *Metrics
    client    *@Svc@Client
    rpcTimeout time.Duration
    limiter    *BucketLimiter
}

func (s *Server) Routes() http.Handler {
    r := chi.NewRouter()
    r.Use(requestID, tracing, s.limiter.Middleware)
    r.Get("/healthz", s.handleHealth)
    r.Get("/readyz", s.handleReady)
    r.Post("/internal/v1/@e@s/{id}/@op@", s.handle@Op@@E@)
    r.Group(func(gr chi.Router) {
        gr.Use(adminOnly)
        gr.Post("/internal/v1/@e@s/reindex", s.handleReindex)
    })
    return middleware.Timeout(@timeout@*time.Millisecond)(r)
}''', _params(r)),

    lambda r: _sub('''func Run@Op@@E@Loop(ctx context.Context, store *@E@Store, out chan<- @E@Event) error {
    ticker := time.NewTicker(@n2@ * time.Millisecond)
    defer ticker.Stop()
    var cursor uint64
    for {
        select {
        case <-ctx.Done():
            return ctx.Err()
        case <-ticker.C:
        }
        events, next, err := store.FetchSince(ctx, cursor, @batch)
        if err != nil {
            if errors.Is(err, ErrStaleCursor) {
                cursor = store.Latest(ctx)
                continue
            }
            return fmt.Errorf("fetch @e@s since %d: %w", cursor, err)
        }
        for _, ev := range events {
            out <- ev
        }
        cursor = next
    }
}''', _params(r)),

    lambda r: _sub('''func (w *@E@Writer) Flush(ctx context.Context) (int, error) {
    w.mu.Lock()
    if len(w.pending) == 0 {
        w.mu.Unlock()
        return 0, nil
    }
    batch := w.pending
    w.pending = make([]@E@Row, 0, w.cap)
    w.mu.Unlock()
    if err := w.db.RunInTx(ctx, func(tx pgx.Tx) error {
        for _, r := range batch {
            if _, err := tx.Exec(ctx, sqlUpsert@E@, r.Key, r.State, r.Payload); err != nil {
                return fmt.Errorf("upsert @e@ %s: %w", r.Key, err)
            }
        }
        return nil
    }); err != nil {
        w.requeue(batch)
        return 0, err
    }
    w.metrics.Observe("@e@.flush_batch", float64(len(batch)))
    return len(batch), nil
}''', _params(r)),

    lambda r: _sub('''func GracefulShutdown(ctx context.Context, srv *http.Server, workers *sync.WaitGroup) error {
    errs := make(chan error, 2)
    go func() {
        if err := srv.Shutdown(ctx); err != nil {
            errs <- fmt.Errorf("http shutdown: %w", err)
        }
    }()
    done := make(chan struct{})
    go func() {
        workers.Wait()
        close(done)
    }()
    select {
    case <-ctx.Done():
        return ctx.Err()
    case err := <-errs:
        return err
    case <-done:
        return nil
    }
}''', _params(r)),
]


def _tabs(lines: list[str]) -> list[str]:
    """Go 语料：行首每 4 空格换 1 个 tab（贴近 gofmt 观感）。"""
    out = []
    for ln in lines:
        stripped = ln.lstrip(" ")
        depth = (len(ln) - len(stripped)) // 4
        out.append("\t" * depth + stripped)
    return out


def _gen_language(r: random.Random, templates, target_lines: int,
                  go_tabs: bool) -> str:
    blocks: list[list[str]] = []
    seen: set[str] = set()
    total = 0
    while total < target_lines:
        block = r.choice(templates)(r)
        assert 8 <= len(block) <= 25, f"块行数越界: {len(block)}"
        text = "\n".join(block)
        if text in seen:
            continue
        seen.add(text)
        if go_tabs:
            block = _tabs(block)
        blocks.append(block)
        total += len(block)
    body = "\n\n".join("\n".join(b) for b in blocks) + "\n"
    for bad in FORBIDDEN:
        assert bad not in body, f"语料含违禁串: {bad}"
    assert not _DOMAIN_RE.search(body), f"语料含域名样式: {_DOMAIN_RE.search(body).group()}"
    assert all(ord(c) < 128 for c in body), "语料必须纯 ASCII"
    assert 400 <= body.count("\n") <= 600, f"总行数越界: {body.count(chr(10))}"
    return body


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    jobs = [
        ("python.txt", PY_TEMPLATES, False),
        ("js.txt", JS_TEMPLATES, False),
        ("go.txt", GO_TEMPLATES, True),
    ]
    for i, (name, templates, go_tabs) in enumerate(jobs):
        r = random.Random(_SEED + i)
        body = _gen_language(r, templates, target_lines=480, go_tabs=go_tabs)
        path = OUT_DIR / name
        path.write_text(body, encoding="utf-8", newline="\n")
        blocks = body.count("\n\n") + 1
        print(f"{path.relative_to(PROJECT_ROOT)}: {body.count(chr(10))} 行, {blocks} 块")
    print("done")


if __name__ == "__main__":
    main()
