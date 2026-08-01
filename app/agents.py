import asyncio
import json
import httpx
import logging
from typing import TypedDict
from langgraph.graph import StateGraph, END, START
from langsmith import traceable
from app.config import Config
from app.retry import with_retry
from app.tools.search import web_search
from app.metrics import record_usage

logger = logging.getLogger(__name__)


class Critique(TypedDict):
    passed: bool
    reasons: list[str]
    missing_queries: list[str]


class ResearchState(TypedDict):
    topic: str
    session_id: str
    session_history: list[dict]  # prior conversation turns passed into agent
    ltm_context: str             # related previous report passed to writer
    sources: list[dict]          # real search results backing the report's facts
    search_results: list[str]
    summaries: list[str]
    report: str
    verified: bool
    critique: Critique | None
    error: str
    iterations: int


async def _tz_call(config: Config, function_name: str, message: str) -> str:
    return await with_retry(
        lambda: _tz_call_once(config, function_name, message),
        max_retries=config.llm_max_retries,
        delay=config.llm_retry_delay,
    )


async def _tz_call_once(config: Config, function_name: str, message: str) -> str:
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            f"{config.tensorzero_url}/inference",
            json={
                "function_name": function_name,
                "input": {"messages": [{"role": "user", "content": message}]},
            },
        )
        response.raise_for_status()
        body = response.json()
        usage = body.get("usage") or {}
        record_usage(usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        return body["content"][0]["text"]


def _dedupe_by_url(sources: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for s in sources:
        url = s.get("url")
        if url and url not in seen:
            seen.add(url)
            deduped.append(s)
    return deduped


def _source_titles(sources: list[dict]) -> str:
    return "; ".join(s.get("title") or s.get("url", "") for s in sources)


def _parse_json_strict(text: str, required_keys: tuple[str, ...] = ("passed", "reasons", "missing_queries")) -> dict:
    """Extracts a single JSON object from a model reply, tolerating stray prose or ```json fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"No JSON object found in model output: {text[:200]!r}")
    parsed = json.loads(cleaned[start:end + 1])
    missing = [k for k in required_keys if k not in parsed]
    if missing:
        raise ValueError(f"JSON missing required keys {missing}: {parsed}")
    return parsed


class SearchAgent:
    """Retrieves real sources via Tavily web search, then has the LLM extract facts ONLY from them."""

    def __init__(self, config: Config):
        self.config = config

    @traceable(run_type="tool", name="agent:plan_queries")
    async def _plan_queries(self, topic: str, session_history: list[dict]) -> list[str]:
        history_ctx = ""
        if session_history:
            recent = session_history[-4:]
            history_ctx = "\n\nPrevious conversation context (use this to understand what the user already knows):\n"
            history_ctx += "\n".join(f"{m['role'].upper()}: {m['content']}" for m in recent)

        raw = await _tz_call(
            self.config,
            "research_summarize",
            "Turn this research topic into 2-3 concrete, distinct web search queries that "
            "together would surface the most current and relevant information. "
            "Reply with ONLY the queries, one per line, no numbering or other text.\n\n"
            f"TOPIC: {topic}{history_ctx}",
        )
        queries = [q.strip(" \t-*0123456789.") for q in raw.splitlines() if q.strip()]
        return queries[:3] or [topic]

    @traceable(run_type="tool", name="agent:search")
    async def run(self, topic: str, session_history: list[dict], queries: list[str] | None = None) -> dict:
        logger.info(f"SearchAgent: researching '{topic}'")
        queries = queries or await self._plan_queries(topic, session_history)

        result_sets = await asyncio.gather(
            *[web_search(self.config, q) for q in queries],
            return_exceptions=True,
        )
        errors = [str(r) for r in result_sets if isinstance(r, Exception)]
        sources = _dedupe_by_url(
            [r for rs in result_sets if not isinstance(rs, Exception) for r in rs]
        )
        if not sources:
            raise RuntimeError(f"All search queries failed for '{topic}': {errors}")

        corpus = "\n\n".join(
            f"[{i + 1}] {s['title']} ({s['url']}, {s.get('published_date', 'n.d.')})\n{s.get('content', '')[:1500]}"
            for i, s in enumerate(sources)
        )
        facts = await _tz_call(
            self.config,
            "research_summarize",
            "Extract the key facts from the sources below.\n"
            "RULES:\n"
            "- Use ONLY information present in the sources. Invent nothing.\n"
            "- Attach a [n] citation (matching the source number) to every factual claim, "
            "placed immediately before the sentence's closing punctuation, e.g. 'X rose 12% [1].'\n"
            "- If the sources do not answer part of the topic, say so explicitly.\n\n"
            f"TOPIC: {topic}\n\nSOURCES:\n{corpus}",
        )
        return {"facts": facts, "sources": sources, "queries": queries}


class SummarizeAgent:
    """Condenses search facts into structured bullet points, preserving [n] citations verbatim."""

    def __init__(self, config: Config):
        self.config = config

    @traceable(run_type="tool", name="agent:summarize")
    async def run(self, search_results: list[str]) -> str:
        logger.info("SummarizeAgent: condensing search results")
        combined = "\n\n".join(search_results)
        return await _tz_call(
            self.config,
            "research_summarize",
            "Summarize these research findings into clear, structured bullet points. "
            "Every [n] citation already present MUST be preserved next to the claim it supports — "
            "never drop or renumber them.\n\n"
            f"{combined}",
        )


class WriterAgent:
    """
    Produces the final structured report, with a References section built from real sources.
    If a related previous report exists in LTM, it uses it as reference. If the previous draft
    was rejected by the critic, revision_notes explains exactly what to fix.
    """

    def __init__(self, config: Config):
        self.config = config

    @traceable(run_type="tool", name="agent:writer")
    async def run(
        self,
        topic: str,
        summaries: list[str],
        ltm_context: str,
        sources: list[dict],
        revision_notes: list[str] | None = None,
    ) -> str:
        logger.info("WriterAgent: drafting report")
        combined = "\n\n".join(summaries)

        ltm_section = ""
        if ltm_context:
            ltm_section = (
                f"\n\nPREVIOUS RESEARCH ON A RELATED TOPIC (use this as reference — "
                f"build on it, correct outdated information, and highlight what has changed):\n"
                f"{ltm_context[:2000]}"
            )

        revision_section = ""
        if revision_notes:
            revision_section = (
                "\n\nAn independent reviewer rejected the previous draft for these reasons — "
                "fix them explicitly in this revision:\n" + "\n".join(f"- {r}" for r in revision_notes)
            )

        report = await _tz_call(
            self.config,
            "report_write",
            f"Write a comprehensive, well-structured research report on: '{topic}'\n\n"
            f"Current research findings (with [n] source citations already attached):\n{combined}"
            f"{ltm_section}{revision_section}\n\n"
            f"Include: Executive Summary, Key Findings, Analysis, and Conclusion. "
            f"Preserve the [n] citations inline, placed immediately before the sentence's "
            f"closing punctuation, e.g. 'X rose 12% [1].'",
        )
        references = "\n".join(f"[{i + 1}] {s['title']} — {s['url']}" for i, s in enumerate(sources))
        return f"{report}\n\n## References\n{references}" if references else report


class CriticAgent:
    """
    Independently reviews the report against the actual sources — using a different model
    than the one that wrote it. Returns structured critique, not a bare pass/fail, so a
    rejected report's retry can search for what was actually missing.
    """

    def __init__(self, config: Config):
        self.config = config

    @traceable(run_type="tool", name="agent:critic")
    async def run(self, topic: str, report: str, sources: list[dict]) -> Critique:
        logger.info("CriticAgent: verifying report")
        try:
            raw = await _tz_call(
                self.config,
                "critic",
                "Review this report.\n\n"
                f"TOPIC: {topic}\n"
                f"SOURCES: {_source_titles(sources)}\n\n"
                f"REPORT:\n{report[:self.config.agent_report_truncate]}",
            )
            critique = _parse_json_strict(raw)
            return Critique(
                passed=bool(critique["passed"]),
                reasons=list(critique.get("reasons") or []),
                missing_queries=list(critique.get("missing_queries") or []),
            )
        except Exception as e:
            # Fail closed: if the critic itself is broken, don't silently accept the report.
            logger.warning(f"Critic call/parse failed, treating report as rejected: {e}")
            return Critique(passed=False, reasons=[f"Critic error: {e}"], missing_queries=[])


class OrchestratorAgent:
    """
    Coordinates all sub-agents. Passes session history to SearchAgent and LTM context
    to WriterAgent so the pipeline is context-aware. If the critic rejects the report,
    the retry searches the critic's own missing_queries and the writer receives the
    critic's reasons as explicit revision instructions (up to agent_max_iterations times).
    """

    def __init__(self, config: Config):
        self.config = config
        self.search_agent = SearchAgent(config)
        self.summarize_agent = SummarizeAgent(config)
        self.writer_agent = WriterAgent(config)
        self.critic_agent = CriticAgent(config)

    @traceable(run_type="chain", name="orchestrator:search")
    async def search_node(self, state: ResearchState) -> dict:
        critique = state.get("critique")
        retry_queries = critique["missing_queries"] if critique and critique.get("missing_queries") else None
        result = await self.search_agent.run(state["topic"], state.get("session_history", []), queries=retry_queries)
        return {"search_results": [result["facts"]], "sources": result["sources"]}

    @traceable(run_type="chain", name="orchestrator:summarize")
    async def summarize_node(self, state: ResearchState) -> dict:
        summary = await self.summarize_agent.run(state["search_results"])
        return {"summaries": [summary]}

    @traceable(run_type="chain", name="orchestrator:write")
    async def write_node(self, state: ResearchState) -> dict:
        critique = state.get("critique")
        revision_notes = critique["reasons"] if critique and not critique.get("passed", True) else None
        report = await self.writer_agent.run(
            state["topic"],
            state["summaries"],
            state.get("ltm_context", ""),
            state.get("sources", []),
            revision_notes=revision_notes,
        )
        return {"report": report, "iterations": state.get("iterations", 0) + 1}

    @traceable(run_type="chain", name="orchestrator:verify")
    async def verify_node(self, state: ResearchState) -> dict:
        critique = await self.critic_agent.run(state["topic"], state["report"], state.get("sources", []))
        return {"verified": critique["passed"], "critique": critique}

    def route(self, state: ResearchState) -> str:
        """Orchestrator decision: retry search with the critic's own follow-up queries, or finish."""
        if not state["verified"] and state.get("iterations", 0) < self.config.agent_max_iterations:
            critique = state.get("critique") or {}
            logger.info(
                f"Critic rejected report (iteration {state['iterations']}): {critique.get('reasons')} "
                f"-> retrying with queries {critique.get('missing_queries')}"
            )
            return "search"
        return END


def build_graph(config: Config):
    orchestrator = OrchestratorAgent(config)
    workflow = StateGraph(ResearchState)

    workflow.add_node("search", orchestrator.search_node)
    workflow.add_node("summarize", orchestrator.summarize_node)
    workflow.add_node("write", orchestrator.write_node)
    workflow.add_node("verify", orchestrator.verify_node)

    workflow.add_edge(START, "search")
    workflow.add_edge("search", "summarize")
    workflow.add_edge("summarize", "write")
    workflow.add_edge("write", "verify")
    workflow.add_conditional_edges(
        "verify",
        orchestrator.route,
        {"search": "search", END: END},
    )

    return workflow.compile()
