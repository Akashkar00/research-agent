import pytest
from app.queue import push_job, get_result, set_result, ensure_group, consume_jobs, ack_job


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
