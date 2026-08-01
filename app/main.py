import asyncio
import uuid
from contextlib import asynccontextmanager
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
from app.cache import cache_count, cache_migrate
from app.guardrails import validate_input
from app.memory import session_add, session_get, db_migrate
from app.queue import push_job, get_result
from app.agents import build_graph
from app.output import generate_pdf, get_report_diff
from app.eval import evaluate_report, run_batch_evaluation, fetch_recent_topics
from app.metrics import read_job_stats

logger = get_logger(__name__)

config = Config()
redis_client: aioredis.Redis = None
graph = None

# Background tasks fired with asyncio.create_task() must be referenced somewhere or
# they can be garbage-collected mid-flight, silently swallowing their exceptions.
_background_tasks: set[asyncio.Task] = set()


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client, graph
    # socket_timeout must exceed the worker's XREADGROUP block=5000 (5s) — otherwise the
    # client's own read timeout fires before the server's blocking wait completes.
    redis_client = await aioredis.from_url(
        config.redis_url, decode_responses=True, socket_timeout=10, socket_connect_timeout=10
    )
    await init_pool(config)
    await db_migrate(config)
    await cache_migrate(config)
    # Only used here for on-demand batch evaluation (/run-evaluation) — the actual research
    # jobs are processed by the separate worker process/service (app/worker.py).
    graph = build_graph(config)
    app.state.config = config
    yield
    await redis_client.aclose()
    await close_pool()


app = FastAPI(title="Research Agent API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[config.allowed_origin] if config.allowed_origin else ["*"],
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

    # The worker is a separate process/ECS service now (app/worker.py) — its own liveness
    # is reflected via a heartbeat key it refreshes every loop iteration, not an in-process
    # counter this API process could ever see directly.
    worker_ok = True
    try:
        worker_ok = await redis_client.get("worker:heartbeat") is not None
    except Exception:
        worker_ok = False

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
        "job_metrics": await read_job_stats(redis_client),
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
