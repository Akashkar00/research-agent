import hashlib
import json
import os
from datetime import datetime, timezone

import fakeredis.aioredis
import pytest
import pytest_asyncio
import respx

# Must run before anything imports app.main, whose module-level `config = Config()`
# would otherwise call real AWS Secrets Manager at import time. conftest.py is always
# loaded before test modules in its directory, so this ordering is guaranteed.
os.environ.setdefault("RESEARCH_AGENT_CONFIG_JSON", json.dumps({
    "AWS_REGION": "us-east-1",
    "BEDROCK_GUARDRAIL_ID": "gr-test",
    "BEDROCK_GUARDRAIL_VERSION": "1",
    "REDIS_URL": "redis://fake",
    "DATABASE_URL": "postgresql://fake",
    "TENSORZERO_URL": "http://tensorzero.test",
    "API_KEY": "test-api-key",
    "TAVILY_API_KEY": "test-tavily-key",
    "RATE_LIMIT_REQUESTS": "10",
    "RATE_LIMIT_WINDOW": "60",
}))


class FakeConfig:
    """A Config-like object built directly in memory, bypassing Secrets Manager entirely
    so tests need no AWS credentials and no network access."""

    def __init__(self, **overrides):
        defaults = dict(
            aws_region="us-east-1",
            bedrock_guardrail_id="gr-test",
            bedrock_guardrail_version="1",
            redis_url="redis://fake",
            database_url="postgresql://fake",
            tensorzero_url="http://tensorzero.test",
            api_key="",
            langsmith_api_key="",
            langchain_project="test",
            langsmith_dataset="test-dataset",
            tavily_api_key="test-tavily-key",
            eval_sample_rate=1.0,
            cache_ttl=3600,
            cache_similarity_threshold=0.85,
            session_ttl=1800,
            session_max_messages=5,
            session_content_truncate=500,
            ltm_days=7,
            ltm_threshold=0.88,
            ltm_diff_threshold=0.7,
            ltm_diff_limit=5,
            ivfflat_lists=100,
            stream_key="research:jobs",
            consumer_group="workers",
            consumer_name="test-consumer",
            result_ttl=3600,
            agent_report_truncate=3000,
            agent_max_iterations=2,
            eval_report_truncate=1500,
            eval_comment_truncate=300,
            llm_max_retries=1,
            llm_retry_delay=0.01,
            rate_limit_requests=10,
            rate_limit_window=60,
            db_pool_min=1,
            db_pool_max=2,
        )
        defaults.update(overrides)
        for k, v in defaults.items():
            setattr(self, k, v)


@pytest.fixture
def config():
    return FakeConfig()


@pytest_asyncio.fixture
async def redis():
    # protocol=2 avoids the RESP3 HELLO handshake, which this fakeredis version doesn't
    # implement — real Redis in production negotiates it fine.
    r = fakeredis.aioredis.FakeRedis(decode_responses=True, protocol=2)
    yield r
    await r.aclose()


@pytest.fixture
def tz_mock():
    """Stubs the TensorZero gateway's /inference endpoint entirely — no real LLM calls."""
    with respx.mock(base_url="http://tensorzero.test", assert_all_called=False) as mock:
        yield mock


def tz_chat_response(text: str) -> dict:
    """Shape of a TensorZero chat-function inference response, as app/agents.py and
    app/eval.py actually parse it: response.json()["content"][0]["text"]."""
    return {"content": [{"type": "text", "text": text}]}


def _fake_vector(text: str, dims: int = 384) -> list[float]:
    """Deterministic pseudo-embedding: same text always maps to the same vector, with
    no real model download required. Not semantically meaningful — tests that need
    controlled similarity should monkeypatch `embed` directly with crafted vectors."""
    h = hashlib.sha256(text.encode()).digest()
    base = [b / 255.0 for b in h]
    return (base * (dims // len(base) + 1))[:dims]


@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch):
    async def _embed(text: str) -> list[float]:
        return _fake_vector(text)

    # Each module imported `embed` by name at import time, so the reference must be
    # patched on every module that holds it, not just on app.embeddings itself.
    monkeypatch.setattr("app.embeddings.embed", _embed)
    monkeypatch.setattr("app.cache.embed", _embed)
    monkeypatch.setattr("app.memory.embed", _embed)
    monkeypatch.setattr("app.output.embed", _embed)
    yield _embed


class FakeRecord(dict):
    def __getitem__(self, key):
        return dict.__getitem__(self, key)


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


class FakeConnection:
    """Understands exactly the SQL shapes app/cache.py and app/memory.py issue, and
    runs the equivalent logic in plain Python instead of a real Postgres+pgvector."""

    def __init__(self, store: dict):
        self.store = store

    async def execute(self, query, *args):
        q = " ".join(query.split())
        if "INSERT INTO reports" in q:
            report_id, topic, report, embedding, created_at = args
            reports = self.store.setdefault("reports", [])
            if not any(r["id"] == report_id for r in reports):
                reports.append({
                    "id": report_id, "topic": topic, "report": report,
                    "embedding": json.loads(embedding), "created_at": created_at,
                })
            return
        if "INSERT INTO semantic_cache" in q:
            key, query_text, result, embedding, expires_at = args
            cache = self.store.setdefault("semantic_cache", {})
            cache[key] = {
                "query": query_text, "result": result,
                "embedding": json.loads(embedding), "expires_at": expires_at,
            }
            return
        # CREATE TABLE / CREATE EXTENSION / CREATE INDEX — no-ops for the fake.
        return

    async def fetchval(self, query, *args):
        q = " ".join(query.split())
        if "SELECT 1" in q:
            return 1
        if "SELECT COUNT(*) FROM semantic_cache" in q:
            now = datetime.now(timezone.utc)
            cache = self.store.get("semantic_cache", {})
            return sum(1 for v in cache.values() if v["expires_at"] > now)
        raise NotImplementedError(q[:80])

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if "FROM semantic_cache" in q:
            embedding, threshold = args
            embedding = json.loads(embedding)
            now = datetime.now(timezone.utc)
            best, best_sim = None, -1.0
            for v in self.store.get("semantic_cache", {}).values():
                if v["expires_at"] <= now:
                    continue
                sim = _cosine(embedding, v["embedding"])
                if sim >= threshold and sim > best_sim:
                    best, best_sim = v, sim
            return FakeRecord(result=best["result"]) if best else None
        if "FROM reports" in q and "similarity" in q:
            embedding, days, threshold = args
            embedding = json.loads(embedding)
            best, best_sim = None, -1.0
            for r in self.store.get("reports", []):
                sim = _cosine(embedding, r["embedding"])
                if sim > threshold and sim > best_sim:
                    best, best_sim = r, sim
            return FakeRecord(**{**best, "similarity": best_sim}) if best else None
        if "FROM reports" in q and "BETWEEN" in q:
            embedding, upper = args
            embedding = json.loads(embedding)
            candidates = []
            for r in self.store.get("reports", []):
                sim = _cosine(embedding, r["embedding"])
                if 0.5 <= sim <= upper:
                    candidates.append((sim, r))
            if not candidates:
                return None
            candidates.sort(key=lambda x: x[1]["created_at"], reverse=True)
            return FakeRecord(report=candidates[0][1]["report"])
        raise NotImplementedError(q[:80])

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if "GROUP BY topic" in q:
            topics = sorted({r["topic"] for r in self.store.get("reports", [])})
            return [FakeRecord(topic=t) for t in topics]
        if "WHERE topic = " in q:
            (topic,) = args
            rows = [r for r in self.store.get("reports", []) if r["topic"] == topic]
            rows.sort(key=lambda r: r["created_at"], reverse=True)
            return [FakeRecord(report=r["report"], created_at=r["created_at"]) for r in rows[:2]]
        raise NotImplementedError(q[:80])


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self):
        self.store: dict = {}

    def acquire(self):
        return FakeAcquire(FakeConnection(self.store))


@pytest.fixture
def fake_pool(monkeypatch):
    pool = FakePool()
    get_pool = lambda: pool  # noqa: E731
    monkeypatch.setattr("app.pool.get_pool", get_pool)
    # cache.py/memory.py imported get_pool by name, so the patch above alone won't
    # reach their already-bound references.
    monkeypatch.setattr("app.cache.get_pool", get_pool, raising=False)
    monkeypatch.setattr("app.memory.get_pool", get_pool, raising=False)
    monkeypatch.setattr("app.output.get_pool", get_pool, raising=False)
    monkeypatch.setattr("app.main.get_pool", get_pool, raising=False)
    return pool
