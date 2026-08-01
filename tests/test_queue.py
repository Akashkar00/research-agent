import pytest
from app.queue import (
    push_job, get_result, set_result, ensure_group, consume_jobs, ack_job,
    claim_stale_jobs, dead_letter, DLQ_MAX_DELIVERIES,
)


@pytest.mark.asyncio
async def test_push_consume_ack_roundtrip(config, redis):
    await ensure_group(redis, config)
    job_id = await push_job(redis, config, "topic X", "session-1", "text")

    jobs = await consume_jobs(redis, config)
    assert len(jobs) == 1
    assert jobs[0]["data"]["job_id"] == job_id
    assert jobs[0]["data"]["topic"] == "topic X"

    # Not yet acked -> pending entries list should show it
    pending = await redis.xpending(config.stream_key, config.consumer_group)
    assert pending["pending"] == 1

    await ack_job(redis, config, jobs[0]["msg_id"])
    pending = await redis.xpending(config.stream_key, config.consumer_group)
    assert pending["pending"] == 0


@pytest.mark.asyncio
async def test_result_roundtrip(config, redis):
    assert await get_result(redis, config, "missing-job") is None
    await set_result(redis, config, "job-1", {"status": "done", "report": "hello"})
    result = await get_result(redis, config, "job-1")
    assert result == {"status": "done", "report": "hello"}


@pytest.mark.asyncio
async def test_ensure_group_is_idempotent(config, redis):
    await ensure_group(redis, config)
    await ensure_group(redis, config)  # must not raise on second call


@pytest.mark.asyncio
async def test_claim_stale_jobs_reclaims_after_idle_timeout(config, redis):
    await ensure_group(redis, config)
    await push_job(redis, config, "stale topic", "session-1", "text")
    await consume_jobs(redis, config)  # read once, never acked — simulates a dead worker

    not_yet_stale = await claim_stale_jobs(redis, config, min_idle_ms=999_999_999)
    assert not_yet_stale == []

    reclaimed = await claim_stale_jobs(redis, config, min_idle_ms=0)
    assert len(reclaimed) == 1
    assert reclaimed[0]["data"]["topic"] == "stale topic"


@pytest.mark.asyncio
async def test_claim_stale_jobs_dead_letters_after_max_deliveries(config, redis):
    await ensure_group(redis, config)
    await push_job(redis, config, "poison message", "session-1", "text")
    await consume_jobs(redis, config)

    # Reclaim it repeatedly without acking — each claim increments the delivery count.
    for _ in range(DLQ_MAX_DELIVERIES):
        await claim_stale_jobs(redis, config, min_idle_ms=0)

    reclaimed = await claim_stale_jobs(redis, config, min_idle_ms=0)
    assert reclaimed == []  # dead-lettered, not handed back for another retry

    pending = await redis.xpending(config.stream_key, config.consumer_group)
    assert pending["pending"] == 0  # dead_letter acks the original

    dlq_entries = await redis.xrange(f"{config.stream_key}:dlq")
    assert len(dlq_entries) == 1
    assert dlq_entries[0][1]["topic"] == "poison message"


@pytest.mark.asyncio
async def test_dead_letter_acks_original_and_writes_dlq(config, redis):
    await ensure_group(redis, config)
    await push_job(redis, config, "topic", "session-1", "text")
    jobs = await consume_jobs(redis, config)
    msg_id = jobs[0]["msg_id"]

    await dead_letter(redis, config, msg_id, jobs[0]["data"])

    pending = await redis.xpending(config.stream_key, config.consumer_group)
    assert pending["pending"] == 0
    dlq_entries = await redis.xrange(f"{config.stream_key}:dlq")
    assert len(dlq_entries) == 1
    assert dlq_entries[0][1]["original_msg_id"] == msg_id
