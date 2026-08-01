import pytest
from app.cache import cache_get, cache_set, cache_count, _cache_key


@pytest.mark.asyncio
async def test_cache_set_then_get_exact_match(config, fake_pool):
    await cache_set(config, "quantum computing 2025", "REPORT A")
    result = await cache_get(config, "quantum computing 2025")
    assert result == "REPORT A"


@pytest.mark.asyncio
async def test_cache_miss_below_threshold(config, fake_pool, monkeypatch):
    async def embed(text):
        vectors = {"topic a": [1.0, 0.0, 0.0], "topic b": [0.0, 1.0, 0.0]}
        return vectors[text]

    monkeypatch.setattr("app.cache.embed", embed)
    await cache_set(config, "topic a", "REPORT A")
    result = await cache_get(config, "topic b")  # orthogonal vector -> similarity 0
    assert result is None


@pytest.mark.asyncio
async def test_cache_hit_above_threshold(config, fake_pool, monkeypatch):
    async def embed(text):
        vectors = {"topic a": [1.0, 0.0, 0.0], "topic a variant": [0.99, 0.01, 0.0]}
        return vectors[text]

    monkeypatch.setattr("app.cache.embed", embed)
    config.cache_similarity_threshold = 0.9
    await cache_set(config, "topic a", "REPORT A")
    result = await cache_get(config, "topic a variant")
    assert result == "REPORT A"


def test_cache_key_is_stable_across_calls():
    # abs(hash(...)) would differ across process restarts (PYTHONHASHSEED is randomized
    # by default) — the fix must be a real content hash, not the builtin hash().
    assert _cache_key("same query") == _cache_key("same query")


def test_cache_key_differs_for_different_queries():
    assert _cache_key("query one") != _cache_key("query two")


@pytest.mark.asyncio
async def test_cache_count_excludes_expired(config, fake_pool):
    await cache_set(config, "fresh topic", "REPORT")
    config.cache_ttl = -10  # already expired
    await cache_set(config, "stale topic", "REPORT")
    assert await cache_count(config) == 1
