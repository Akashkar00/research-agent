# IMPROVEMENTS — 7.5 → 8.5+

> **Supersedes** the previous 6.5 → 8.5 roadmap (Tier 1 + Tier 2), which is complete.
> That version is in git history (`git show 21db10b:IMPROVEMENTS.md`); `PROGRESS.md`
> tracks it. This file is the current roadmap only.

**Current honest score: 7.5/10.** Not 8.0–8.2. The infrastructure is genuinely
8+ — separate worker service, `XAUTOCLAIM` + DLQ, queue-depth autoscaling, OIDC CI
with rollback, behavioural tests that assert the *right* things. The score is held
down by four things, in order of how much they cost you:

1. **The evaluation story is a shell.** 30/30 golden-set rows have `human_quality: null`.
   Five judges, a Spearman function, a batch runner — and zero evidence any judge score
   correlates with reality. For a GenAI role this is the discipline being hired for.
2. **Every number the system can measure is unpublished.** `/stats.job_metrics` computes
   p50/p95/cost/hit-rate. The red-team harness runs 16 attacks. None of it is in the README.
3. **A correctness bug in the flagship critic loop** that actively degrades the citation
   integrity the whole project claims to guarantee (§B.1).
4. **Live security no-ops** — `API_KEY` is empty in Secrets Manager, so auth returns early
   on a public plaintext-HTTP endpoint that spends OpenAI money.

Nothing below is a rewrite. It's ~1.5 days of work total, and the first two sections
alone move the score more than everything else combined.

---

# TIER A — The score is actually here (7.5 → 8.3)

## A.1 Label the golden set and publish the correlation

**Why this is #1:** the project's headline claim is "LLM evaluation on every request."
An interviewer asks *"how do you know your judge is right?"* and today the answer is
"I don't." Everything else in this file is engineering polish; this is the one gap
that's specifically about the job title.

**Do:**

1. Run the pipeline over the golden set to generate reports:
   ```bash
   python -m evals.run_golden          # writes evals/golden_results.json
   ```
2. Read **10–15** of the generated reports yourself. Score each `human_quality` 1–5 in
   `evals/golden_set.jsonl`. Judge on: are the facts actually in the cited source, is
   the analysis non-obvious, would a business analyst use this.
3. Re-run `python -m evals.run_golden`. It prints the Spearman correlation once ≥5 rows
   are labeled ([evals/run_golden.py:108](evals/run_golden.py#L108)).
4. Put the number in the README **whatever it is**. `run_golden.py` already tells you to
   report a weak correlation honestly rather than omit it — do exactly that.

**Do not** batch-label from memory or let an LLM fill the labels. A fabricated human
label defeats the entire metric, and it's the one number a sharp interviewer will
probe hardest.

**Done when:** README contains a line like
`Judge–human agreement (Spearman, n=12): 0.61` — and you can explain what a 0.61
means and where the judge disagreed with you.

**Bonus, cheap:** note the two or three topics where the judge scored high and you
scored low. "My judge over-rewards structure and under-penalizes thin sourcing" is a
*much* stronger interview answer than a good correlation number.

---

## A.2 Publish the numbers the system already computes

You built the instrumentation and reported nothing from it. That reads as "never
actually run under load."

**Add a `## Measured Results` section to the README** with:

| Number | Where it comes from |
|---|---|
| p50 / p95 job latency | `GET /stats` → `job_metrics.p50_latency_s` / `p95_latency_s` |
| Mean cost per report | `job_metrics.mean_cost_usd` (flag the GPT-4o-blended caveat already in [app/metrics.py:7-12](app/metrics.py#L7-L12)) |
| Cache + LTM hit rate | `job_metrics.cache_hit_rate`, `ltm_hit_rate` |
| n (sample size) | `job_metrics.n` — state it; 12 jobs is not 1,200 and pretending otherwise is the overclaim you avoid everywhere else |
| Mean citation support rate | `run_golden.py` prints it |
| Red-team results | 16 attacks × 4 categories → `blocked / passed / errored / timeout` table from the harness's `/results` |

Generate real traffic first — 20–30 varied topics through `/research`, then read `/stats`.

**Done when:** every claim in the README's feature table has a number behind it, and
the red-team table shows real outcomes including any category that *passed* (i.e. got
through). A harness that reports 16/16 blocked is less credible than one that reports
13/16 and explains the three.

---

# TIER B — Real bugs (fix before an interviewer finds them)

## B.1 Fix state accumulation in the critic retry loop — **correctness bug**

`ResearchState` is a plain `TypedDict` with no reducers, so LangGraph **overwrites** on
every node return. When the critic rejects and
[app/agents.py:274-278](app/agents.py#L274-L278) re-runs search with only
`missing_queries`, the fields `sources`, `search_results` and `summaries` are all
*replaced*, not accumulated.

Consequence: the revised report is written from the follow-up queries **alone** — the
original research is thrown away — and `## References` is rebuilt from a smaller,
different source set while the model is still instructed to preserve the `[n]` markers
from the *previous* corpus. Your critic loop degrades the exact citation integrity the
project is built to guarantee.

**Fix:**

```python
import operator
from typing import Annotated

class ResearchState(TypedDict):
    ...
    sources: Annotated[list[dict], operator.add]
    search_results: Annotated[list[str], operator.add]
    summaries: Annotated[list[str], operator.add]
```

Then de-dupe in `search_node` — `_dedupe_by_url` already exists — so a URL returned by
both passes doesn't produce two reference entries and shift every `[n]` after it.

**Also fix the test that let this through.**
[tests/test_agents.py:80](tests/test_agents.py#L80) asserts only that the two queries
*differ*. It validates the mechanism and misses the semantics. Add:

```python
# The retry must ADD to the original research, not replace it.
assert len(final["sources"]) == 2          # one from each pass, deduped
assert len(final["summaries"]) == 2
# Every [n] in the body must resolve to a real reference line.
body, refs = final["report"].split("## References")
for n in set(re.findall(r"\[(\d+)\]", body)):
    assert f"[{n}]" in refs
```

That last assertion is the one worth having — it's a citation-integrity invariant, and
it's the kind of test that says you understand what the system is *for*.

---

## B.2 Set a real `API_KEY`

[app/auth.py:6](app/auth.py#L6) returns early when `config.api_key` is empty, and the
deployed secret is empty. Auth is currently a no-op on a public endpoint with no TLS
that spends OpenAI money at 10 req/min/IP.

```bash
aws secretsmanager get-secret-value --secret-id research-agent/config \
  --query SecretString --output text > /tmp/cfg.json
# edit API_KEY in /tmp/cfg.json, then:
aws secretsmanager put-secret-value --secret-id research-agent/config \
  --secret-string file:///tmp/cfg.json
aws ecs update-service --cluster research-agent-cluster \
  --service research-agent-app --force-new-deployment
aws ecs update-service --cluster research-agent-cluster \
  --service research-agent-pyrit --force-new-deployment
rm /tmp/cfg.json
```

The `ignore_changes = [secret_string]` lifecycle you added means Terraform won't stomp
this — that fix is doing its job here.

**Caveat to document, not hide:** without HTTPS the key travels in cleartext. Keep it
in Known Limitations next to the ACM entry. An API key over plain HTTP is a rate-limit
identity, not a security boundary — say so.

---

## B.3 Stop swallowing every exception in `ensure_group`

[app/queue.py:27-31](app/queue.py#L27-L31) — a bare `except: pass` makes a Redis outage
indistinguishable from the expected `BUSYGROUP` on restart. The worker then loops
silently against a broker it can't reach.

```python
async def ensure_group(redis, config) -> None:
    try:
        await redis.xgroup_create(config.stream_key, config.consumer_group, id="0", mkstream=True)
    except aioredis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise          # a real failure must not look like a normal restart
```

---

## B.4 Decouple `/health` from TensorZero liveness

[app/main.py:485](app/main.py#L485) returns 503 if TensorZero is unreachable, so the ALB
deregisters **every** task — a gateway blip takes down `/result/{job_id}` reads and the
frontend, neither of which needs the LLM gateway.

Split the concerns: report `tensorzero` status in the body (observability) but only fail
the ALB health check on `redis` + `database` (the deps every request path needs). Keep
the worker heartbeat out of the app's own liveness too — a dead worker should page you,
not delete the API.

**Interview value:** "I distinguish liveness from dependency health" is a real SRE
answer, and it's a one-line change.

---

## B.5 Guard the semantic cache against false-positive hits

`cache_similarity_threshold = 0.85` over MiniLM *topic* embeddings will collide
"Fed rate policy 2025" with "Fed rate policy 2024" and serve a stale report as fresh.
There is no test for a false-positive hit — only for a true one.

**Do:**
- Add a test in `tests/test_cache.py` that stores under one topic and asserts a *near*
  but semantically distinct topic misses (monkeypatch `embed` with crafted vectors —
  `conftest.py` already tells you to do exactly this).
- Raise the threshold to ~0.93 for the cache specifically, or add a cheap year/entity
  guard. Justify whichever you pick in the README's Architecture Decisions — the
  reasoning matters more than the number.
- Note the growth issue: `_cache_key` hashes the *exact* query while lookup is semantic,
  so N phrasings of one topic write N rows, evicted only by TTL. Either add a periodic
  `DELETE FROM semantic_cache WHERE expires_at < NOW()` or say so in Known Limitations.

---

# TIER C — Defensibility (zero code, highest cost-per-hour if skipped)

11 commits, ~5,700 lines, 1,247 of them Terraform, in 3 days. Heavy AI assistance is
obvious and completely fine — everyone works this way now. The risk isn't "did you
build this," it's **"why is it built this way."** If the depth outruns your ability to
defend it, the depth works against you.

Write short answers to these and make sure they're *yours*:

- Why `min_idle_ms=120_000` in `claim_stale_jobs`, and what breaks at 10s or at 10min?
- Why `DLQ_MAX_DELIVERIES = 3`?
- `ivfflat` vs `hnsw` for pgvector — why, and at what row count does the choice flip?
- Why `lists = 100` on the ivfflat index? (The usual heuristic is ~rows/1000 — what does
  that imply about your current row count?)
- Redis Streams vs SQS — you have this written; can you also say what would make you switch?
- Why `all-MiniLM-L6-v2` (384d) and what you'd lose/gain at 768d.
- Why the critic runs on a different vendor than the writer, and what specific failure
  mode that prevents.
- Where does this break at 100 concurrent jobs? (Real answer: Tavily rate limits,
  `db_pool_max=10` per task, and no LangGraph checkpointer, so any crash replays every
  paid call.)

Also fix the one doc drift: README's OIDC section still says the sub claim is
`repo:OWNER/REPO:ref:refs/heads/main`, but commit `ee6b8da` established this account's
tokens embed **numeric owner/repo IDs**. Correct it — that finding is one of your best
stories and the README currently contradicts it.

---

# TIER D — Beyond 8.5 (a separate project, not a gap in this one)

Explicitly out of scope for reaching 8.5. Listed so "not started" reads as a decision
rather than an oversight:

- LangGraph `PostgresSaver` checkpointer — a worker crash mid-graph currently replays
  every paid LLM call from zero. Highest-value item here.
- Streaming responses (SSE) — biggest perceived-latency win.
- Prompt A/B via TensorZero variants, scored against the judge from A.1. The natural
  sequel: once your judge is validated, it becomes an optimization signal.
- Per-session cost guardrail.
- Reranking before the writer.
- NAT Gateway + private subnets (~$32–35/mo) and a domain for real ACM HTTPS — both
  cost/tradeoff calls already documented honestly in Known Limitations.

---

# Effort summary

| Item | Effort | Score impact |
|---|---|---|
| A.1 Label golden set + publish Spearman | ~90 min | **Highest** |
| A.2 Publish measured results + red-team table | ~1 hr | **High** |
| B.1 State accumulation fix + invariant test | ~45 min | High (real bug) |
| B.2 Set `API_KEY` | ~10 min | Medium |
| B.3 `ensure_group` exception handling | ~5 min | Low |
| B.4 Decouple `/health` | ~15 min | Medium (good interview answer) |
| B.5 Cache false-positive guard + test | ~40 min | Medium |
| C Defensibility prep | ~2 hrs | **Highest at interview** |

Roughly **1.5 days**, most of it not writing code.

---

# Definition of done for 8.5

- [ ] ≥10 golden-set rows hand-labeled; real Spearman number in the README
- [ ] README has a `## Measured Results` section: p50/p95, cost/report, hit rates, n
- [ ] Red-team table published including any attack that got through
- [ ] Critic retry accumulates sources; citation-integrity invariant is under test
- [ ] `API_KEY` set in Secrets Manager; cleartext-over-HTTP caveat documented
- [ ] `/health` fails the ALB check only on hard dependencies
- [ ] A test proves a semantically-near-but-distinct topic *misses* the cache
- [ ] README OIDC section matches the numeric-sub-claim finding in `ee6b8da`
- [ ] You can answer every question in Tier C without looking anything up

---

## The one-line version

The infrastructure is already 8+. Prove the output is good (A.1), publish what you
measured (A.2), fix the retry loop that quietly breaks your own citation guarantee
(B.1) — and be able to defend all of it out loud.
