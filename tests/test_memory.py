import pytest
from app.memory import ltm_store, ltm_diff, ltm_search, _report_id


@pytest.mark.asyncio
async def test_ltm_store_is_idempotent(config, fake_pool):
    # Storing the exact same (topic, report) twice must not create two rows — that was
    # the bug: a fresh uuid4() every call meant ON CONFLICT never fired.
    await ltm_store(config, "AI regulation", "Report text v1")
    await ltm_store(config, "AI regulation", "Report text v1")
    assert len(fake_pool.store["reports"]) == 1


@pytest.mark.asyncio
async def test_ltm_store_different_reports_create_different_rows(config, fake_pool):
    await ltm_store(config, "AI regulation", "Report text v1")
    await ltm_store(config, "AI regulation", "Report text v2 — materially different")
    assert len(fake_pool.store["reports"]) == 2


def test_report_id_is_a_content_hash_not_random():
    assert _report_id("topic", "report") == _report_id("topic", "report")
    assert _report_id("topic", "report v1") != _report_id("topic", "report v2")


@pytest.mark.asyncio
async def test_ltm_diff_on_two_different_reports_is_non_empty(config, fake_pool):
    await ltm_store(config, "AI regulation", "Line one.\nLine two.\n")
    await ltm_store(config, "AI regulation", "Line one.\nLine two, but changed.\n")
    diff = await ltm_diff(config, "AI regulation")
    assert diff is not None
    assert "changed" in diff


@pytest.mark.asyncio
async def test_ltm_diff_with_only_one_report_is_none(config, fake_pool):
    await ltm_store(config, "solo topic", "Only report.")
    diff = await ltm_diff(config, "solo topic")
    assert diff is None


@pytest.mark.asyncio
async def test_ltm_search_respects_threshold(config, fake_pool, monkeypatch):
    async def embed(text):
        return {"topic": [1.0, 0.0], "topic query": [0.5, 0.5]}[text]

    monkeypatch.setattr("app.memory.embed", embed)
    await ltm_store(config, "topic", "Stored report")
    config.ltm_threshold = 0.95
    hit = await ltm_search(config, "topic query")
    assert hit is None  # 0.5/0.5 direction is not similar enough to 1.0/0.0 at this threshold
