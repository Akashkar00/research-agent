import contextvars
import json
from datetime import datetime, timezone
import redis.asyncio as aioredis

# Approximate blended rate — the writer path (research_summarize/report_write) runs on
# GPT-4o; judge/critic calls run on Groq Llama, which is roughly two orders of magnitude
# cheaper and contributes negligibly to the total. This is an estimate for a cost-per-report
# ballpark, not a billing-accurate figure (actual cost depends on which provider TensorZero's
# fallback routing actually used for a given call).
GPT4O_INPUT_PER_1M = 2.50
GPT4O_OUTPUT_PER_1M = 10.00

JOB_METRICS_KEY = "metrics:jobs"
JOB_METRICS_MAX = 200

_usage_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar("usage", default=None)


def start_tracking() -> None:
    _usage_ctx.set({"input_tokens": 0, "output_tokens": 0, "calls": 0})


def record_usage(input_tokens: int, output_tokens: int) -> None:
    """Called from every TensorZero response — a no-op if no job is currently tracking
    (e.g. a batch-eval run outside the request path)."""
    acc = _usage_ctx.get()
    if acc is not None:
        acc["input_tokens"] += input_tokens
        acc["output_tokens"] += output_tokens
        acc["calls"] += 1


def get_usage() -> dict:
    return _usage_ctx.get() or {"input_tokens": 0, "output_tokens": 0, "calls": 0}


def estimate_cost_usd(usage: dict) -> float:
    return round(
        usage["input_tokens"] / 1_000_000 * GPT4O_INPUT_PER_1M
        + usage["output_tokens"] / 1_000_000 * GPT4O_OUTPUT_PER_1M,
        4,
    )


async def record_job_metric(redis: aioredis.Redis, latency_s: float, usage: dict, source: str) -> None:
    """source: 'cache' | 'ltm' | 'fresh' — which path served the job, for hit-rate stats."""
    await redis.rpush(JOB_METRICS_KEY, json.dumps({
        "latency_s": round(latency_s, 2),
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cost_usd": estimate_cost_usd(usage),
        "source": source,
        "ts": datetime.now(timezone.utc).isoformat(),
    }))
    await redis.ltrim(JOB_METRICS_KEY, -JOB_METRICS_MAX, -1)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(len(s) * pct))
    return round(s[idx], 2)


async def read_job_stats(redis: aioredis.Redis) -> dict:
    raw = await redis.lrange(JOB_METRICS_KEY, 0, -1)
    entries = [json.loads(m) for m in raw]
    if not entries:
        return {"n": 0}
    latencies = [e["latency_s"] for e in entries]
    costs = [e["cost_usd"] for e in entries]
    sources = [e["source"] for e in entries]
    return {
        "n": len(entries),
        "p50_latency_s": _percentile(latencies, 0.50),
        "p95_latency_s": _percentile(latencies, 0.95),
        "mean_cost_usd": round(sum(costs) / len(costs), 4),
        "cache_hit_rate": round(sources.count("cache") / len(sources), 2),
        "ltm_hit_rate": round(sources.count("ltm") / len(sources), 2),
    }
