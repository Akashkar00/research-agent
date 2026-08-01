import json
import uuid
import redis.asyncio as aioredis
from app.config import Config


async def push_job(redis: aioredis.Redis, config: Config, topic: str, session_id: str, output_format: str) -> str:
    job_id = str(uuid.uuid4())
    await redis.xadd(config.stream_key, {
        "job_id": job_id,
        "topic": topic,
        "session_id": session_id,
        "output_format": output_format,
    })
    return job_id


async def get_result(redis: aioredis.Redis, config: Config, job_id: str) -> dict | None:
    data = await redis.get(f"result:{job_id}")
    return json.loads(data) if data else None


async def set_result(redis: aioredis.Redis, config: Config, job_id: str, result: dict) -> None:
    await redis.setex(f"result:{job_id}", config.result_ttl, json.dumps(result))


async def ensure_group(redis: aioredis.Redis, config: Config) -> None:
    try:
        await redis.xgroup_create(config.stream_key, config.consumer_group, id="0", mkstream=True)
    except Exception:
        pass


async def consume_jobs(redis: aioredis.Redis, config: Config) -> list[dict]:
    messages = await redis.xreadgroup(
        config.consumer_group,
        config.consumer_name,
        {config.stream_key: ">"},
        count=1,
        block=5000,
    )
    if not messages:
        return []
    jobs = []
    for _, entries in messages:
        for msg_id, data in entries:
            jobs.append({"msg_id": msg_id, "data": data})
    return jobs


async def ack_job(redis: aioredis.Redis, config: Config, msg_id: str) -> None:
    await redis.xack(config.stream_key, config.consumer_group, msg_id)


DLQ_MAX_DELIVERIES = 3


async def dead_letter(redis: aioredis.Redis, config: Config, msg_id: str, data: dict) -> None:
    """Moves a message that has failed too many times to a separate dead-letter stream
    and acks the original, so it stops being reclaimed forever."""
    await redis.xadd(f"{config.stream_key}:dlq", {**data, "original_msg_id": msg_id})
    await ack_job(redis, config, msg_id)


async def claim_stale_jobs(redis: aioredis.Redis, config: Config, min_idle_ms: int = 120_000) -> list[dict]:
    """XAUTOCLAIM: reclaims messages that have sat unacked longer than min_idle_ms — the
    worker that read them died mid-job — so they don't strand forever. A message that has
    already been delivered DLQ_MAX_DELIVERIES times is dead-lettered instead of retried again."""
    _next_id, messages, _deleted = await redis.xautoclaim(
        config.stream_key, config.consumer_group, config.consumer_name,
        min_idle_time=min_idle_ms, start_id="0-0", count=10,
    )
    claimed = []
    for msg_id, data in messages:
        pending = await redis.xpending_range(
            config.stream_key, config.consumer_group, min=msg_id, max=msg_id, count=1
        )
        delivery_count = pending[0]["times_delivered"] if pending else 1
        if delivery_count > DLQ_MAX_DELIVERIES:
            await dead_letter(redis, config, msg_id, data)
            continue
        claimed.append({"msg_id": msg_id, "data": data})
    return claimed
