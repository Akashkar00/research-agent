"""
Runs the full multi-agent pipeline over evals/golden_set.jsonl and reports:

  - Judge-human agreement: Spearman correlation between the judge's overall_quality
    score and the golden set's human_quality label. Requires human_quality to have
    been hand-filled in for at least a handful of entries (it ships as `null` — a
    fabricated "human" label would defeat the entire point of this metric).
  - Citation support rate: fraction of report sentences carrying a [n] citation.
  - Coverage: fraction of each entry's must_mention terms actually present.

Run from the project root with the app's real dependencies installed:
    TAVILY key, TensorZero reachable, Postgres/Redis reachable (same as production).

    python -m evals.run_golden
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents import build_graph, ResearchState
from app.config import Config
from app.eval import eval_quality, citation_support_rate


def spearman(x: list[float], y: list[float]) -> float:
    def rank(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        r = [0.0] * len(vals)
        for position, idx in enumerate(order):
            r[idx] = position
        return r

    rx, ry = rank(x), rank(y)
    n = len(x)
    mean_rx, mean_ry = sum(rx) / n, sum(ry) / n
    cov = sum((a - mean_rx) * (b - mean_ry) for a, b in zip(rx, ry))
    var_x = sum((a - mean_rx) ** 2 for a in rx)
    var_y = sum((b - mean_ry) ** 2 for b in ry)
    return cov / (var_x * var_y) ** 0.5 if var_x and var_y else 0.0


def coverage(report: str, must_mention: list[str]) -> float:
    if not must_mention:
        return 1.0
    lower = report.lower()
    hits = sum(1 for term in must_mention if term.lower() in lower)
    return round(hits / len(must_mention), 2)


def violations(report: str, must_not_mention: list[str]) -> list[str]:
    lower = report.lower()
    return [term for term in must_not_mention if term.lower() in lower]


async def main():
    config = Config()
    graph = build_graph(config)
    golden_path = Path(__file__).parent / "golden_set.jsonl"
    entries = [json.loads(line) for line in golden_path.read_text().splitlines() if line.strip()]

    results = []
    for entry in entries:
        topic = entry["topic"]
        print(f"Running: {topic}")
        state = ResearchState(
            topic=topic, session_id="golden-eval", session_history=[], ltm_context="",
            sources=[], search_results=[], summaries=[], report="",
            verified=False, critique=None, error="", iterations=0,
        )
        try:
            final = await graph.ainvoke(state)
            report = final["report"]
        except Exception as e:
            print(f"  FAILED: {e}")
            results.append({"topic": topic, "error": str(e)})
            continue

        judge = await eval_quality(config, topic, report)
        results.append({
            "topic": topic,
            "judge_overall_quality": judge["score"],
            "citation_support_rate": citation_support_rate(report),
            "coverage": coverage(report, entry.get("must_mention", [])),
            "violations": violations(report, entry.get("must_not_mention", [])),
            "human_quality": entry.get("human_quality"),
        })

    ok = [r for r in results if "error" not in r]
    print("\n=== Golden Set Results ===")
    print(f"Ran {len(ok)}/{len(results)} topics successfully")
    if ok:
        print(f"Mean citation support rate: {sum(r['citation_support_rate'] for r in ok) / len(ok):.2f}")
        print(f"Mean coverage: {sum(r['coverage'] for r in ok) / len(ok):.2f}")
        total_violations = sum(len(r["violations"]) for r in ok)
        print(f"must_not_mention violations: {total_violations}")

    labeled = [r for r in ok if r["human_quality"] is not None and r["judge_overall_quality"] is not None]
    if len(labeled) >= 5:
        judge_scores = [r["judge_overall_quality"] for r in labeled]
        human_scores = [r["human_quality"] for r in labeled]
        corr = spearman(judge_scores, human_scores)
        print(f"Judge-human agreement (Spearman, n={len(labeled)}): {corr:.2f}")
        if corr < 0.5:
            print("NOTE: correlation is weak — report this number honestly in the README rather than omitting it.")
    else:
        print(
            f"Judge-human agreement: N/A — only {len(labeled)} entries have a human_quality label. "
            "Read the generated reports above and fill in evals/golden_set.jsonl's human_quality "
            "field (1-5) by hand for at least 5-10 entries to get a real correlation number."
        )

    out_path = Path(__file__).parent / "golden_results.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nFull results written to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
