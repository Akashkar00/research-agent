import asyncio
import random
import uuid
import logging
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import httpx
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import Response, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import redis.asyncio as aioredis

from app.logger import get_logger
from app.config import Config
from app.pool import init_pool, close_pool, get_pool
from app.auth import require_api_key
from app.cache import cache_get, cache_set, cache_migrate, cache_count
from app.guardrails import validate_input, validate_output
from app.memory import session_add, session_get, ltm_search, ltm_search_related, ltm_store, ltm_diff, db_migrate
from app.queue import push_job, get_result, set_result, ensure_group, consume_jobs, ack_job
from app.agents import build_graph, ResearchState
from app.output import generate_pdf, generate_json_report, get_report_diff
from app.eval import evaluate_report, run_batch_evaluation, fetch_recent_topics

logger = get_logger(__name__)

config = Config()
redis_client: aioredis.Redis = None
graph = None

# Background tasks fired with asyncio.create_task() must be referenced somewhere or
# they can be garbage-collected mid-flight, silently swallowing their exceptions.
_background_tasks: set[asyncio.Task] = set()

_worker_consecutive_failures = 0
_WORKER_DEGRADED_THRESHOLD = 5


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


async def _rate_limit(request: Request) -> None:
    # Behind the ALB, request.client.host is the ALB itself, not the caller — that would
    # make the rate limit global instead of per-client. The ALB is our only trusted proxy
    # hop and always APPENDS the connection it received to X-Forwarded-For, so the
    # rightmost entry is the one it observed directly; anything to the left of that could
    # be spoofed by the client itself.
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        client_ip = xff.split(",")[-1].strip()
    elif request.client:
        client_ip = request.client.host
    else:
        client_ip = "unknown"
    key = f"ratelimit:{client_ip}"
    count = await redis_client.incr(key)
    if count == 1:
        await redis_client.expire(key, config.rate_limit_window)
    if count > config.rate_limit_requests:
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")


async def _worker_loop():
    global _worker_consecutive_failures
    await ensure_group(redis_client, config)
    while True:
        try:
            jobs = await consume_jobs(redis_client, config)
            _worker_consecutive_failures = 0
            for job in jobs:
                spawn_background(_process_job(job["data"], job["msg_id"]))
        except Exception:
            _worker_consecutive_failures += 1
            logger.error(
                f"Worker loop error (consecutive failures: {_worker_consecutive_failures}): "
                f"{traceback.format_exc()}"
            )
            await asyncio.sleep(1)


async def _process_job(data: dict, msg_id: str):
    job_id = data["job_id"]
    topic = data["topic"]
    session_id = data["session_id"]
    output_format = data.get("output_format", "text")

    base_log = get_logger("job")
    log = logging.LoggerAdapter(base_log, extra={"job_id": job_id, "session_id": session_id, "topic": topic})

    try:
        log.info(f"Starting job for topic: {topic}")

        # Fetch session history before any branch — agent always receives it
        session_history = await session_get(redis_client, session_id)

        cached = await cache_get(config, topic)
        if cached:
            log.info("Cache hit")
            report_text = cached
        else:
            ltm_hit = await ltm_search(config, topic)
            if ltm_hit:
                log.info("LTM hit")
                report_text = ltm_hit["report"]
            else:
                log.info("Running multi-agent pipeline")
                # Find a related (not identical) previous report for the writer to reference
                ltm_context = await ltm_search_related(config, topic) or ""
                if ltm_context:
                    log.info("Found related LTM context for writer agent")
                state = ResearchState(
                    topic=topic,
                    session_id=session_id,
                    session_history=session_history,  # agent is now context-aware
                    ltm_context=ltm_context,           # writer builds on prior research
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
                # Store only on fresh generation — storing on every cache/LTM hit too meant
                # ON CONFLICT never fired (each call used a fresh random id) and duplicate
                # rows piled up, silently killing the diff feature (it compared a report to
                # itself). ltm_store now derives a deterministic id from (topic, report).
                await ltm_store(config, topic, report_text)

        await session_add(redis_client, config, session_id, "assistant", report_text[:config.session_content_truncate])
        diff = await ltm_diff(config, topic)
        result: dict = {"status": "done", "topic": topic, "report": report_text, "diff": diff}

        # Automatic per-job evaluation is sampled, not run on every request — the full
        # judge suite always runs against the golden set instead (evals/run_golden.py).
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client, graph
    # socket_timeout must exceed consume_jobs' XREADGROUP block=5000 (5s) — otherwise the
    # client's own read timeout fires before the server's blocking wait completes, and an
    # idle queue makes every single poll fail with a spurious redis.exceptions.TimeoutError.
    redis_client = await aioredis.from_url(
        config.redis_url, decode_responses=True, socket_timeout=10, socket_connect_timeout=10
    )
    await init_pool(config)
    await db_migrate(config)
    await cache_migrate(config)
    graph = build_graph(config)
    app.state.config = config
    spawn_background(_worker_loop())
    yield
    await redis_client.aclose()
    await close_pool()


app = FastAPI(title="Research Agent API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class ResearchRequest(BaseModel):
    topic: str
    session_id: str = ""
    output_format: str = "text"


@app.get("/")
async def frontend():
    return FileResponse("/app/index.html")


@app.get("/health")
async def health():
    redis_ok = True
    try:
        await redis_client.ping()
    except Exception:
        redis_ok = False

    db_ok = True
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception:
        db_ok = False

    tensorzero_ok = True
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(f"{config.tensorzero_url}/health")
            # 404 just means this gateway version has no /health route — the process
            # still answered HTTP, which is what we actually care about here. Only
            # timeouts, connection errors, and 5xx count as unreachable.
            tensorzero_ok = r.status_code < 500
    except Exception:
        tensorzero_ok = False

    worker_ok = _worker_consecutive_failures < _WORKER_DEGRADED_THRESHOLD

    # Any dependency down -> 503, so the ALB deregisters this task instead of routing
    # traffic to one whose DB pool or LLM gateway is actually unreachable.
    healthy = redis_ok and db_ok and tensorzero_ok and worker_ok
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "degraded",
            "redis": "ok" if redis_ok else "error",
            "database": "ok" if db_ok else "error",
            "tensorzero": "ok" if tensorzero_ok else "error",
            "worker": "ok" if worker_ok else "error",
        },
    )


@app.post("/research", dependencies=[Depends(require_api_key), Depends(_rate_limit)])
async def start_research(req: ResearchRequest):
    ok, reason = await validate_input(config, req.topic)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)
    session_id = req.session_id or str(uuid.uuid4())
    await session_add(redis_client, config, session_id, "user", req.topic)
    job_id = await push_job(redis_client, config, req.topic, session_id, req.output_format)
    return {"job_id": job_id, "session_id": session_id}


@app.get("/result/{job_id}", dependencies=[Depends(require_api_key)])
async def get_job_result(job_id: str):
    result = await get_result(redis_client, config, job_id)
    if result is None:
        return {"status": "pending"}
    return result


@app.get("/session/{session_id}", dependencies=[Depends(require_api_key)])
async def get_session(session_id: str):
    messages = await session_get(redis_client, session_id)
    return {"session_id": session_id, "messages": messages}


@app.get("/diff/{topic}", dependencies=[Depends(require_api_key)])
async def report_diff(topic: str):
    diff = await get_report_diff(config, topic)
    return {"topic": topic, "diff": diff or "No previous report found."}


@app.get("/result/{job_id}/pdf", dependencies=[Depends(require_api_key)])
async def download_pdf(job_id: str):
    result = await get_result(redis_client, config, job_id)
    if not result or result.get("status") != "done":
        raise HTTPException(status_code=404, detail="Report not ready")
    pdf_bytes = generate_pdf(result.get("topic", "Report"), result["report"])
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={job_id}.pdf"},
    )


@app.get("/stats", dependencies=[Depends(require_api_key)])
async def stats():
    info = await redis_client.info()
    keys = await redis_client.dbsize()
    session_keys = len([k async for k in redis_client.scan_iter("session:*")])
    return {
        "redis": {
            "total_keys": keys,
            "active_sessions": session_keys,
            "memory_used_mb": round(info["used_memory"] / 1024 / 1024, 2),
            "connected_clients": info["connected_clients"],
            "uptime_hours": round(info["uptime_in_seconds"] / 3600, 1),
        },
        "cache_entries": await cache_count(config),
        "tensorzero_url": config.tensorzero_url,
        "guardrail_id": config.bedrock_guardrail_id,
    }


@app.get("/evaluate/{job_id}", dependencies=[Depends(require_api_key)])
async def evaluate_job(job_id: str):
    result = await get_result(redis_client, config, job_id)
    if not result or result.get("status") != "done":
        raise HTTPException(status_code=404, detail="Job not done yet")
    scores = await evaluate_report(config, job_id, result["topic"], result["report"])
    return {"job_id": job_id, "topic": result["topic"], "scores": scores}


class BatchEvalRequest(BaseModel):
    topics: list[str] = []


@app.post("/run-evaluation", dependencies=[Depends(require_api_key)])
async def trigger_batch_evaluation(req: BatchEvalRequest):
    topics = req.topics if req.topics else await fetch_recent_topics()
    if not topics:
        raise HTTPException(status_code=400, detail="No topics found. Submit at least one research job first.")
    spawn_background(run_batch_evaluation(config, graph, topics))
    return {"message": "Batch evaluation started in background", "topics": len(topics)}
