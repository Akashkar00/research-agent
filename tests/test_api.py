import httpx
import pytest
from app import main as main_module


@pytest.fixture
async def client(redis, fake_pool, monkeypatch):
    monkeypatch.setattr(main_module, "redis_client", redis)
    monkeypatch.setattr(main_module, "graph", object())  # unused by the routes under test
    monkeypatch.setattr(main_module, "_worker_consecutive_failures", 0)
    # Normally set inside lifespan(), which we don't run in these route-level tests.
    main_module.app.state.config = main_module.config
    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_research_requires_api_key(client):
    r = await client.post("/research", json={"topic": "x"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_research_with_valid_key_succeeds(client, monkeypatch):
    async def fake_validate_input(cfg, text):
        return True, ""

    monkeypatch.setattr(main_module, "validate_input", fake_validate_input)

    r = await client.post(
        "/research", json={"topic": "x"}, headers={"X-API-Key": main_module.config.api_key}
    )
    assert r.status_code == 200
    assert "job_id" in r.json()


@pytest.mark.asyncio
async def test_rate_limit_returns_429_past_the_limit(client, monkeypatch):
    async def fake_validate_input(cfg, text):
        return True, ""

    monkeypatch.setattr(main_module, "validate_input", fake_validate_input)
    monkeypatch.setattr(main_module.config, "rate_limit_requests", 2)

    headers = {"X-API-Key": main_module.config.api_key}
    r1 = await client.post("/research", json={"topic": "x"}, headers=headers)
    r2 = await client.post("/research", json={"topic": "x"}, headers=headers)
    r3 = await client.post("/research", json={"topic": "x"}, headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429


@pytest.mark.asyncio
async def test_rate_limit_key_uses_rightmost_xff_hop_not_leftmost(client, monkeypatch):
    """Behind the ALB, the rightmost X-Forwarded-For entry is the one the ALB itself
    observed; the leftmost can be spoofed by the client. Two different attacker-supplied
    leftmost values with the SAME real (rightmost) IP must share one rate-limit bucket."""
    async def fake_validate_input(cfg, text):
        return True, ""

    monkeypatch.setattr(main_module, "validate_input", fake_validate_input)
    monkeypatch.setattr(main_module.config, "rate_limit_requests", 1)

    headers_a = {"X-API-Key": main_module.config.api_key, "X-Forwarded-For": "1.1.1.1, 203.0.113.9"}
    headers_b = {"X-API-Key": main_module.config.api_key, "X-Forwarded-For": "2.2.2.2, 203.0.113.9"}
    r1 = await client.post("/research", json={"topic": "x"}, headers=headers_a)
    r2 = await client.post("/research", json={"topic": "x"}, headers=headers_b)
    assert r1.status_code == 200
    assert r2.status_code == 429  # same real client IP (rightmost hop) -> shared bucket


@pytest.mark.asyncio
async def test_health_degraded_when_redis_down(client, monkeypatch):
    class _DeadRedis:
        async def ping(self):
            raise ConnectionError("redis down")

    monkeypatch.setattr(main_module, "redis_client", _DeadRedis())
    r = await client.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["redis"] == "error"


@pytest.mark.asyncio
async def test_health_ok_when_all_dependencies_up(client, tz_mock):
    tz_mock.get("/health").mock(return_value=httpx.Response(200))
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
