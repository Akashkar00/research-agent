import asyncio
import json
from contextlib import suppress
import httpx
import pytest
from app import worker as worker_module
from app.queue import ensure_group
from tests.conftest import tz_chat_response


@pytest.fixture
def wire_worker(config, redis, fake_pool, monkeypatch):
    monkeypatch.setattr(worker_module, "redis_client", redis)
    monkeypatch.setattr(worker_module, "config", config)
    return worker_module


@pytest.mark.asyncio
async def test_process_job_cache_hit_skips_pipeline(wire_worker, monkeypatch):
    async def fake_cache_get(cfg, topic):
        return "CACHED REPORT"

    async def fail_if_called(*a, **kw):
        raise AssertionError("graph should not run on a cache hit")

    monkeypatch.setattr(worker_module, "cache_get", fake_cache_get)
    monkeypatch.setattr(worker_module, "graph", type("G", (), {"ainvoke": fail_if_called})())

    await ensure_group(wire_worker.redis_client, wire_worker.config)
    data = {"job_id": "job-1", "topic": "cached topic", "session_id": "s1", "output_format": "text"}
    await worker_module._process_job(data, msg_id="1-1")

    result = json.loads(await wire_worker.redis_client.get("result:job-1"))
    assert result["status"] == "done"
    assert result["report"] == "CACHED REPORT"

    metrics = await wire_worker.redis_client.lrange("metrics:jobs", 0, -1)
    assert len(metrics) == 1
    assert json.loads(metrics[0])["source"] == "cache"


@pytest.mark.asyncio
async def test_process_job_fresh_generation_runs_graph_and_records_metrics(
    wire_worker, monkeypatch, tz_mock
):
    async def fake_cache_get(cfg, topic):
        return None

    async def fake_ltm_search(cfg, topic):
        return None

    async def fake_ltm_search_related(cfg, topic):
        return ""

    async def noop(*a, **kw):
        return None

    async def fake_validate_output(cfg, text):
        return True, ""

    monkeypatch.setattr(worker_module, "cache_get", fake_cache_get)
    monkeypatch.setattr(worker_module, "ltm_search", fake_ltm_search)
    monkeypatch.setattr(worker_module, "ltm_search_related", fake_ltm_search_related)
    monkeypatch.setattr(worker_module, "cache_set", noop)
    monkeypatch.setattr(worker_module, "ltm_store", noop)
    async def fake_ltm_diff(cfg, topic):
        return None

    monkeypatch.setattr(worker_module, "ltm_diff", fake_ltm_diff)
    monkeypatch.setattr(worker_module, "validate_output", fake_validate_output)
    monkeypatch.setattr(worker_module.config, "eval_sample_rate", 0.0)  # skip background eval

    async def fake_web_search(cfg, query, max_results=6):
        return [{"title": "Source", "url": "https://example.com/1", "content": "facts", "published_date": "2026-01-01"}]

    monkeypatch.setattr("app.agents.web_search", fake_web_search)

    def inference_response(request):
        body = json.loads(request.content)
        fn = body["function_name"]
        msg = body["input"]["messages"][0]["content"]
        if fn == "research_summarize":
            if "search queries" in msg:
                return httpx.Response(200, json=tz_chat_response("q"))
            return httpx.Response(200, json=tz_chat_response("Fact [1]."))
        if fn == "report_write":
            return httpx.Response(200, json=tz_chat_response("Executive Summary...\nKey Findings...\nAnalysis...\nConclusion..."))
        if fn == "critic":
            return httpx.Response(200, json=tz_chat_response(json.dumps({"passed": True, "reasons": [], "missing_queries": []})))
        raise AssertionError(fn)

    tz_mock.post("/inference").mock(side_effect=inference_response)

    from app.agents import build_graph
    monkeypatch.setattr(worker_module, "graph", build_graph(wire_worker.config))

    await ensure_group(wire_worker.redis_client, wire_worker.config)
    data = {"job_id": "job-2", "topic": "fresh topic", "session_id": "s2", "output_format": "text"}
    await worker_module._process_job(data, msg_id="1-2")

    result = json.loads(await wire_worker.redis_client.get("result:job-2"))
    assert result["status"] == "done"
    assert "## References" in result["report"]

    metrics = await wire_worker.redis_client.lrange("metrics:jobs", 0, -1)
    assert json.loads(metrics[0])["source"] == "fresh"


@pytest.mark.asyncio
async def test_worker_loop_writes_heartbeat(wire_worker, monkeypatch):
    # _worker_loop runs forever by design (it logs and continues past errors rather
    # than raising) — let it run briefly, then cancel it, instead of expecting it to exit.
    async def fake_claim_stale_jobs(redis, config, **kw):
        return []

    async def fake_consume_jobs(redis, config):
        # A real consume_jobs blocks on Redis for up to 5s per call — without some delay
        # here the loop spins as fast as the interpreter allows and can starve the test's
        # own sleep/cancel from ever being scheduled.
        await asyncio.sleep(0.01)
        return []

    monkeypatch.setattr(worker_module, "claim_stale_jobs", fake_claim_stale_jobs)
    monkeypatch.setattr(worker_module, "consume_jobs", fake_consume_jobs)

    task = asyncio.create_task(worker_module._worker_loop())
    await asyncio.sleep(0.1)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    heartbeat = await wire_worker.redis_client.get(worker_module.HEARTBEAT_KEY)
    assert heartbeat is not None
