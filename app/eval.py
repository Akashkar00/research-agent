import asyncio
import re
import httpx
import logging
from langsmith import Client, traceable
from app.config import Config
from app.retry import with_retry
from app.metrics import record_usage

logger = logging.getLogger(__name__)


_ls_client: Client | None = None


def _ls() -> Client:
    global _ls_client
    if _ls_client is None:
        _ls_client = Client()
    return _ls_client


def _parse_score(text: str) -> float | None:
    """Returns None on parse failure — never fabricates a score. Callers must treat
    None as "unknown", not as a real mediocre result."""
    m = re.search(r"SCORE:\s*(\d+(?:\.\d+)?)\s*/\s*10", text, re.IGNORECASE)
    return round(float(m.group(1)) / 10.0, 2) if m else None


def citation_support_rate(report: str) -> float:
    """Fraction of substantive sentences (outside the References section) that carry
    at least one [n] citation marker. Deterministic — not graded by an LLM."""
    body = report.split("## References")[0]
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if len(s.strip()) > 20]
    if not sentences:
        return 0.0
    cited = sum(1 for s in sentences if re.search(r"\[\d+\]", s))
    return round(cited / len(sentences), 2)


async def _judge(config: Config, prompt: str) -> str:
    return await with_retry(
        lambda: _judge_once(config, prompt),
        max_retries=config.llm_max_retries,
        delay=config.llm_retry_delay,
    )


async def _judge_once(config: Config, prompt: str) -> str:
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{config.tensorzero_url}/inference",
            json={
                # Dedicated judge model (Groq Llama) — a different vendor and model than
                # GPT-4o, which writes the report. The judge never grades its own work.
                "function_name": "judge",
                "input": {"messages": [{"role": "user", "content": prompt}]},
            },
        )
        r.raise_for_status()
        body = r.json()
        usage = body.get("usage") or {}
        record_usage(usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        return body["content"][0]["text"]


@traceable(run_type="chain", name="eval:relevance")
async def eval_relevance(config: Config, topic: str, report: str) -> dict:
    verdict = await _judge(
        config,
        f"Rate how relevant this research report is to the topic '{topic}'.\n"
        f"Reply with exactly: SCORE: X/10 on the first line, then one sentence reason.\n\n"
        f"Report:\n{report[:config.eval_report_truncate]}",
    )
    score = _parse_score(verdict)
    return {"key": "relevance", "score": score, "comment": verdict[:config.eval_comment_truncate]}


@traceable(run_type="chain", name="eval:completeness")
async def eval_completeness(config: Config, report: str) -> dict:
    verdict = await _judge(
        config,
        f"Does this research report contain all four required sections: "
        f"Executive Summary, Key Findings, Analysis, and Conclusion?\n"
        f"Reply with exactly: SCORE: X/10 on the first line, then one sentence reason.\n\n"
        f"Report:\n{report[:config.eval_report_truncate]}",
    )
    score = _parse_score(verdict)
    return {"key": "completeness", "score": score, "comment": verdict[:config.eval_comment_truncate]}


@traceable(run_type="chain", name="eval:groundedness")
async def eval_groundedness(config: Config, topic: str, report: str) -> dict:
    """10 = fully grounded (higher is always better here — unlike the old hallucination_risk
    metric, which scored 10 = worst and was never marked as such anywhere in the dataset)."""
    verdict = await _judge(
        config,
        f"Check this report on '{topic}' for groundedness — whether its claims are backed "
        f"by real evidence rather than fabricated statistics, impossible dates, or claims "
        f"contradicting well-known facts.\n"
        f"Score: 10/10 = fully grounded, zero hallucinations. 1/10 = mostly fabricated.\n"
        f"Reply with exactly: SCORE: X/10 on the first line, then list any suspicious claims.\n\n"
        f"Report:\n{report[:config.eval_report_truncate]}",
    )
    score = _parse_score(verdict)
    return {"key": "groundedness", "score": score, "comment": verdict[:config.eval_comment_truncate]}


@traceable(run_type="chain", name="eval:overall_quality")
async def eval_quality(config: Config, topic: str, report: str) -> dict:
    verdict = await _judge(
        config,
        f"Rate the overall quality of this research report on '{topic}'.\n"
        f"Consider: depth of analysis, factual accuracy, writing clarity, logical structure, "
        f"and practical usefulness to a business analyst.\n"
        f"Reply with exactly: SCORE: X/10 on the first line, then two sentences explaining the rating.\n\n"
        f"Report:\n{report[:config.eval_report_truncate]}",
    )
    score = _parse_score(verdict)
    return {"key": "overall_quality", "score": score, "comment": verdict[:config.eval_comment_truncate]}


@traceable(run_type="chain", name="eval:citation_support")
async def eval_citation_support(config: Config, report: str) -> dict:
    rate = citation_support_rate(report)
    return {"key": "citation_support", "score": rate, "comment": f"{rate * 100:.0f}% of sentences carry a [n] citation"}


@traceable(run_type="chain", name="evaluate-report")
async def evaluate_report(config: Config, job_id: str, topic: str, report: str) -> dict:
    """Runs all judges in parallel. Sampling (whether this runs at all for a given job)
    is decided by the caller via config.eval_sample_rate — this always runs the full suite."""
    results = await asyncio.gather(
        eval_relevance(config, topic, report),
        eval_completeness(config, report),
        eval_groundedness(config, topic, report),
        eval_quality(config, topic, report),
        eval_citation_support(config, report),
    )
    scores = {r["key"]: r["score"] for r in results if r["score"] is not None}
    parse_failures = [r["key"] for r in results if r["score"] is None]
    parse_failure_rate = round(len(parse_failures) / len(results), 2)
    if parse_failures:
        logger.warning(f"Judge parse failures for job {job_id}: {parse_failures}")

    try:
        client = _ls()
        try:
            dataset = client.read_dataset(dataset_name=config.langsmith_dataset)
        except Exception:
            dataset = client.create_dataset(
                config.langsmith_dataset,
                description="Research agent LLM-as-judge evaluation results",
            )
        client.create_example(
            inputs={"topic": topic},
            outputs={"report_preview": report[:400]},
            dataset_id=dataset.id,
            metadata={
                "job_id": job_id,
                "judge_parse_failure_rate": parse_failure_rate,
                "parse_failed_metrics": parse_failures,
                **scores,
            },
        )
    except Exception as e:
        logger.warning(f"LangSmith logging failed for job {job_id}: {e}")
    return {**scores, "judge_parse_failure_rate": parse_failure_rate}


async def fetch_recent_topics(limit: int = 10) -> list[str]:
    """Pull distinct topics from the reports table — real user queries, nothing hardcoded."""
    from app.pool import get_pool
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT topic FROM reports GROUP BY topic ORDER BY MAX(created_at) DESC LIMIT $1",
            limit,
        )
        return [row["topic"] for row in rows]


async def run_batch_evaluation(config: Config, graph, topics: list[str]) -> list[dict]:
    from app.agents import ResearchState
    from app.memory import ltm_search_related
    results = []
    for topic in topics:
        ltm_context = await ltm_search_related(config, topic) or ""
        state = ResearchState(
            topic=topic, session_id="batch-eval",
            session_history=[],
            ltm_context=ltm_context,
            sources=[],
            search_results=[], summaries=[], report="",
            verified=False, critique=None, error="", iterations=0,
        )
        final = await graph.ainvoke(state)
        scores = await evaluate_report(config, f"batch-{topic[:20]}", topic, final["report"])
        results.append({"topic": topic, "scores": scores})
    return results
