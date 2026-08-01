import json
import httpx
import pytest
from app.agents import build_graph, ResearchState, SearchAgent
from tests.conftest import tz_chat_response


def _fake_search(calls: list):
    async def _search(cfg, query, max_results=6):
        calls.append(query)
        return [{
            "title": f"Result for {query}",
            "url": f"https://example.com/{abs(hash(query)) % 100000}",
            "content": "Some factual content backing the claim.",
            "published_date": "2026-01-01",
        }]
    return _search


def _fresh_state(topic="test topic") -> ResearchState:
    return ResearchState(
        topic=topic, session_id="s1", session_history=[], ltm_context="",
        sources=[], search_results=[], summaries=[], report="",
        verified=False, critique=None, error="", iterations=0,
    )


@pytest.mark.asyncio
async def test_search_agent_fails_loudly_when_all_queries_fail(config, monkeypatch):
    async def failing_search(cfg, query, max_results=6):
        raise RuntimeError("Tavily 401 Unauthorized")

    monkeypatch.setattr("app.agents.web_search", failing_search)
    agent = SearchAgent(config)
    # No silent fallback to model recall — killing the search key must surface as an error.
    with pytest.raises(RuntimeError):
        await agent.run("some topic", [], queries=["q1", "q2"])


@pytest.mark.asyncio
async def test_graph_runs_end_to_end_with_stubbed_gateway(config, tz_mock, monkeypatch):
    calls = []
    monkeypatch.setattr("app.agents.web_search", _fake_search(calls))

    def inference_response(request):
        body = json.loads(request.content)
        fn = body["function_name"]
        msg = body["input"]["messages"][0]["content"]
        if fn == "research_summarize":
            if "search queries" in msg:
                return httpx.Response(200, json=tz_chat_response("query one\nquery two"))
            return httpx.Response(200, json=tz_chat_response("Fact one. [1]\nFact two. [1]"))
        if fn == "report_write":
            return httpx.Response(200, json=tz_chat_response(
                "Executive Summary...\nKey Findings...\nAnalysis...\nConclusion..."
            ))
        if fn == "critic":
            return httpx.Response(200, json=tz_chat_response(
                json.dumps({"passed": True, "reasons": [], "missing_queries": []})
            ))
        raise AssertionError(f"unexpected function {fn}")

    tz_mock.post("/inference").mock(side_effect=inference_response)

    graph = build_graph(config)
    final = await graph.ainvoke(_fresh_state())

    assert final["verified"] is True
    assert final["iterations"] == 1
    assert "## References" in final["report"]
    assert len(calls) == 2  # one query for each planned search


@pytest.mark.asyncio
async def test_critic_rejection_retries_with_different_search_queries(config, tz_mock, monkeypatch):
    calls = []
    monkeypatch.setattr("app.agents.web_search", _fake_search(calls))
    critic_calls = {"n": 0}

    def inference_response(request):
        body = json.loads(request.content)
        fn = body["function_name"]
        msg = body["input"]["messages"][0]["content"]
        if fn == "research_summarize":
            if "search queries" in msg:
                return httpx.Response(200, json=tz_chat_response("original query"))
            return httpx.Response(200, json=tz_chat_response("Fact one. [1]"))
        if fn == "report_write":
            return httpx.Response(200, json=tz_chat_response(
                "Executive Summary...\nKey Findings...\nAnalysis...\nConclusion..."
            ))
        if fn == "critic":
            critic_calls["n"] += 1
            if critic_calls["n"] == 1:
                return httpx.Response(200, json=tz_chat_response(json.dumps({
                    "passed": False,
                    "reasons": ["Missing recent data on the topic"],
                    "missing_queries": ["follow-up query"],
                })))
            return httpx.Response(200, json=tz_chat_response(json.dumps({
                "passed": True, "reasons": [], "missing_queries": [],
            })))
        raise AssertionError(fn)

    tz_mock.post("/inference").mock(side_effect=inference_response)

    graph = build_graph(config)
    final = await graph.ainvoke(_fresh_state())

    assert final["verified"] is True
    assert final["iterations"] == 2
    # A no-op retry loop would issue the identical query twice; this must differ.
    assert calls[0] == "original query"
    assert calls[1] == "follow-up query"
    assert calls[0] != calls[1]


@pytest.mark.asyncio
async def test_critic_call_failure_fails_closed_not_open(config, tz_mock, monkeypatch):
    """If the critic itself can't be reached/parsed, the report must be treated as
    rejected, never silently accepted."""
    calls = []
    monkeypatch.setattr("app.agents.web_search", _fake_search(calls))
    config.agent_max_iterations = 0  # don't actually retry, just check the single verdict

    def inference_response(request):
        body = json.loads(request.content)
        fn = body["function_name"]
        msg = body["input"]["messages"][0]["content"]
        if fn == "research_summarize":
            if "search queries" in msg:
                return httpx.Response(200, json=tz_chat_response("q"))
            return httpx.Response(200, json=tz_chat_response("Fact one. [1]"))
        if fn == "report_write":
            return httpx.Response(200, json=tz_chat_response("A report."))
        if fn == "critic":
            return httpx.Response(200, json=tz_chat_response("not valid json at all"))
        raise AssertionError(fn)

    tz_mock.post("/inference").mock(side_effect=inference_response)

    graph = build_graph(config)
    final = await graph.ainvoke(_fresh_state())
    assert final["verified"] is False
    assert final["critique"]["passed"] is False
