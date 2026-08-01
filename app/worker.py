"""
Standalone worker process — consumes research jobs from the Redis Streams queue and
runs the multi-agent pipeline. Runs as its own ECS service (research-agent-worker),
decoupled from the API process: the API serves HTTP and scales on request count; this
scales on queue depth. See app/main.py for the API process and app/queue.py for the
claim-check (XAUTOCLAIM) and dead-letter logic.

Entry point: python -m app.worker
"""
import asyncio
import random
import time
import logging
import traceback
from contextlib import suppress
from datetime import datetime, timezone
import boto3
import redis.asyncio as aioredis

from app.logger import get_logger
from app.config import Config
from app.pool import init_pool, close_pool
from app.cache import cache_get, cache_set, cache_migrate
from app.guardrails import validate_output
from app.memory import session_add, session_get, ltm_search, ltm_search_related, ltm_store, ltm_diff, db_migrate
from app.queue import set_result, ensure_group, consume_jobs, ack_job, claim_stale_jobs
from app.agents import build_graph, ResearchState
from app.output import generate_pdf, generate_json_report
from app.eval import evaluate_report
from app.metrics import start_tracking, get_usage, record_job_metric

logger = get_logger("worker")

config = Config()
redis_client: aioredis.Redis = None
graph = None

_background_tasks: set[asyncio.Task] = set()

HEARTBEAT_KEY = "worker:heartbeat"
HEARTBEAT_TTL = 30  # seconds — app/main.py's /health treats a missing/expired key as degraded

STREAM_METRIC_INTERVAL = 30  # seconds between publishing queue depth to CloudWatch


def spawn_background(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_log_background_task_result)
    return task


def _log_background_task_result(task: asyncio.Task) -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error(f"Background task failed: {exc}", exc_info=exc)


async def _process_job(data: dict, msg_id: str):
    job_id = data["job_id"]
    topic = data["topic"]
    session_id = data["session_id"]
    output_format = data.get("output_format", "text")

    base_log = get_logger("job")
    log = logging.LoggerAdapter(base_log, extra={"job_id": job_id, "session_id": session_id, "topic": topic})
    job_start = time.monotonic()
    start_tracking()
    source = "fresh"

    try:
        log.info(f"Starting job for topic: {topic}")

        session_history = await session_get(redis_client, session_id)

        cached = await cache_get(config, topic)
        if cached:
            log.info("Cache hit")
            report_text = cached
            source = "cache"
        else:
            ltm_hit = await ltm_search(config, topic)
            if ltm_hit:
                log.info("LTM hit")
                report_text = ltm_hit["report"]
                source = "ltm"
            else:
                log.info("Running multi-agent pipeline")
                ltm_context = await ltm_search_related(config, topic) or ""
                if ltm_context:
                    log.info("Found related LTM context for writer agent")
                state = ResearchState(
                    topic=topic,
                    session_id=session_id,
                    session_history=session_history,
                    ltm_context=ltm_context,
                    sources=[],
                    search_results=[],
                    summaries=[],
                    report="",
                    verified=False,
                    critique=None,
                    error="",
                    iterations=0,
                )
                final_state = await graph.ainvoke(state)
                report_text = final_state["report"]
                ok, reason = await validate_output(config, report_text)
                if not ok:
                    await set_result(redis_client, config, job_id, {"status": "blocked", "error": reason})
                    await ack_job(redis_client, config, msg_id)
                    return
                await cache_set(config, topic, report_text)
                await ltm_store(config, topic, report_text)

        await session_add(redis_client, config, session_id, "assistant", report_text[:config.session_content_truncate])
        diff = await ltm_diff(config, topic)
        result: dict = {"status": "done", "topic": topic, "report": report_text, "diff": diff}

        if random.random() < config.eval_sample_rate:
            spawn_background(evaluate_report(config, job_id, topic, report_text))

        if output_format == "pdf":
            pdf_bytes = generate_pdf(topic, report_text)
            result["pdf_base64"] = __import__("base64").b64encode(pdf_bytes).decode()
        elif output_format == "json":
            result["structured"] = generate_json_report(topic, report_text, job_id, datetime.now(timezone.utc))

        await set_result(redis_client, config, job_id, result)
        log.info("Job completed successfully")
    except Exception as e:
        log.error(f"Job failed: {traceback.format_exc()}")
        await set_result(redis_client, config, job_id, {"status": "error", "error": str(e)})
    finally:
        await ack_job(redis_client, config, msg_id)
        await record_job_metric(redis_client, time.monotonic() - job_start, get_usage(), source)


async def _publish_queue_depth_loop():
    """Publishes Redis stream length (XLEN) as a CloudWatch custom metric so the worker
    ECS service can target-track its own autoscaling on actual queue depth, not CPU."""
    cw = boto3.client("cloudwatch", region_name=config.aws_region)
    while True:
        try:
            depth = await redis_client.xlen(config.stream_key)
            await asyncio.to_thread(
                cw.put_metric_data,
                Namespace="ResearchAgent",
                MetricData=[{
                    "MetricName": "QueueDepth",
                    "Value": float(depth),
                    "Unit": "Count",
                }],
            )
        except Exception as e:
            logger.warning(f"Failed to publish queue depth metric: {e}")
        await asyncio.sleep(STREAM_METRIC_INTERVAL)


async def _worker_loop():
    await ensure_group(redis_client, config)
    while True:
        try:
            reclaimed = await claim_stale_jobs(redis_client, config)
            for job in reclaimed:
                log_msg = f"Reclaiming stale job {job['data'].get('job_id')} from a dead worker"
                logger.warning(log_msg)
                spawn_background(_process_job(job["data"], job["msg_id"]))

            jobs = await consume_jobs(redis_client, config)
            for job in jobs:
                spawn_background(_process_job(job["data"], job["msg_id"]))

            await redis_client.set(HEARTBEAT_KEY, datetime.now(timezone.utc).isoformat(), ex=HEARTBEAT_TTL)
        except Exception:
            logger.error(f"Worker loop error: {traceback.format_exc()}")
            await asyncio.sleep(1)


async def main():
    global redis_client, graph
    redis_client = await aioredis.from_url(
        config.redis_url, decode_responses=True, socket_timeout=10, socket_connect_timeout=10
    )
    await init_pool(config)
    await db_migrate(config)
    await cache_migrate(config)
    graph = build_graph(config)
    logger.info("Worker started")

    metric_task = spawn_background(_publish_queue_depth_loop())
    try:
        await _worker_loop()
    finally:
        metric_task.cancel()
        with suppress(asyncio.CancelledError):
            await metric_task
        await redis_client.aclose()
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
