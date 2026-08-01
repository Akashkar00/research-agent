# IMPROVEMENTS — 6.5 → 8.5

Concrete work plan to raise this project from "impressive infra, thin GenAI substance"
to "a fresher project that survives a senior interviewer's questions."

Current score: **6.5 / 10** for a fresher GenAI engineer role.
Target after Tier 1 + Tier 2: **8.5 / 10**.

Nothing below adds a new AWS service. Every item either makes an existing claim true,
fixes a real bug, or replaces a decorative feature with a working one.

---

## Where the 3.5 points are being lost

| Area | Current | Why points are lost | Points back |
|---|---|---|---|
| Grounding / retrieval | 4 LLM prompts, no search, no citations | The "research agent" recalls, it doesn't research | **+0.8** |
| Evaluation integrity | Same model writes and judges, no ground truth | Metrics are not evidence of anything | **+0.6** |
| Tests | Zero. CI deploys untested code | Contradicts the whole "production rigor" pitch | **+0.5** |
| Correctness bugs | Blocking event loop, broken diff, broken rate limit | Bugs a reviewer finds in 20 min of reading | **+0.4** |
| Agent design | Critic loop is a no-op; no tools | LangGraph used where a for-loop would do | **+0.4** |
| Security / infra honesty | Public subnets, no encryption, unauthenticated PyRIT | Design intent contradicts deployment | **+0.5** |
| README accuracy | Overclaims PyRIT, "search", deletion protection | Erodes trust in everything else in the doc | **+0.3** |

Tier 1 and Tier 2 below map onto these rows.

---

# TIER 1 — Non-negotiable (6.5 → ~7.8)

## 1.1 Give the search agent an actual search tool

**Problem.** `SearchAgent.run()` in [agents.py:314](app/agents.py#L314) prompts the LLM to
"find 5 key facts." There is no search. Every report is model recall bounded by the
training cutoff, presented to the user as current research.

**Fix.** Add a real web search provider and make it the *only* source of facts.

Add to `requirements.txt`:
```
tavily-python==0.5.0
```

New file `app/tools/search.py`:

```python
import asyncio
import httpx
from app.config import Config
from app.retry import with_retry


async def web_search(config: Config, query: str, max_results: int = 6) -> list[dict]:
    """
    Returns [{"title", "url", "content", "published_date"}].
    This is the ONLY source of facts in the pipeline. If it returns nothing,
    the job fails loudly rather than silently falling back to model recall.
    """
    async def _once():
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": config.tavily_api_key,
                    "query": query,
                    "search_depth": "advanced",
                    "max_results": max_results,
                    "include_raw_content": False,
                },
            )
            r.raise_for_status()
            return r.json()["results"]

    results = await with_retry(_once, max_retries=config.llm_max_retries,
                               delay=config.llm_retry_delay)
    if not results:
        raise RuntimeError(f"No search results for query: {query}")
    return results
```

Rewrite `SearchAgent` so the LLM only *selects and condenses* retrieved text — it
never supplies facts of its own:

```python
class SearchAgent:
    """Retrieves real sources, then has the LLM extract facts ONLY from them."""

    @traceable(run_type="tool", name="agent:search")
    async def run(self, topic: str, session_history: list[dict]) -> dict:
        # Let the model turn a vague topic into 2-3 concrete search queries
        queries = await self._plan_queries(topic, session_history)

        result_sets = await asyncio.gather(
            *[web_search(self.config, q) for q in queries],
            return_exceptions=True,
        )
        sources = _dedupe_by_url(
            [r for rs in result_sets if not isinstance(rs, Exception) for r in rs]
        )
        if not sources:
            raise RuntimeError("All search queries failed")

        corpus = "\n\n".join(
            f"[{i+1}] {s['title']} ({s['url']}, {s.get('published_date','n.d.')})\n{s['content']}"
            for i, s in enumerate(sources)
        )
        facts = await _tz_call(
            self.config,
            "research_summarize",
            "Extract the key facts from the sources below.\n"
            "RULES:\n"
            "- Use ONLY information present in the sources. Invent nothing.\n"
            "- Attach a [n] citation to every factual claim.\n"
            "- If the sources do not answer part of the topic, say so explicitly.\n\n"
            f"TOPIC: {topic}\n\nSOURCES:\n{corpus}",
        )
        return {"facts": facts, "sources": sources}
```

Then thread `sources` through `ResearchState` and into the writer's prompt, and
append a **References** section to every report. Update the `report_write`
minijinja system template to require inline `[n]` citations.

**Acceptance criteria**
- Every generated report ends with a References list of real, resolvable URLs.
- Killing the Tavily key makes jobs fail with a clear error — it does **not**
  silently degrade to model recall.
- A new `eval:citation_support` judge (see 1.3) scores what fraction of claims
  carry a citation.

**Effort:** ~4 hours. **This is the single highest-value item in the document.**

---

## 1.2 Make the critic loop actually do something

**Problem.** `route()` in [agents.py:440](app/agents.py#L440) sends a rejected report back
to `search` with identical state and an identical prompt. The critic's reasoning is
discarded — `CriticAgent.run()` returns a bare `bool`. Iteration 2 reproduces
iteration 1. A retry loop that cannot change its input is decoration.

**Fix.** Return structured critique and feed it forward.

```python
class Critique(TypedDict):
    passed: bool
    reasons: list[str]        # what's wrong
    missing_queries: list[str]  # what to search for on the retry


class CriticAgent:
    @traceable(run_type="tool", name="agent:critic")
    async def run(self, topic: str, report: str, sources: list[dict]) -> Critique:
        raw = await _tz_call(
            self.config, "critic",   # dedicated TZ function, see 1.3
            "Review this report. Check: (a) every claim is supported by the listed "
            "sources, (b) all four required sections are present, (c) no unsupported "
            "numbers or dates.\n"
            "Reply as JSON: {\"passed\": bool, \"reasons\": [...], "
            "\"missing_queries\": [...]}\n\n"
            f"TOPIC: {topic}\nSOURCES: {_source_titles(sources)}\n\nREPORT:\n{report}"
        )
        return _parse_json_strict(raw, Critique)
```

Store `critique` in `ResearchState`. On retry, `SearchAgent` runs
`critique["missing_queries"]` instead of the original query, and `WriterAgent`
receives `critique["reasons"]` as explicit revision instructions. Cap at
`agent_max_iterations` as today.

**Acceptance criteria**
- A LangSmith trace of a rejected run shows *different* search queries on
  iteration 2 than iteration 1.
- The writer prompt on iteration 2 visibly contains the critic's reasons.

**Effort:** ~2 hours.

---

## 1.3 Stop letting the model grade its own homework

**Problem.** [eval.py](app/eval.py) sends every judge prompt to `research_summarize` —
the same TensorZero function, same GPT-4o, that wrote the report. The
hallucination-risk metric is produced by the source of the hallucinations. On top of
that, `_parse_score` returns `0.5` when the regex misses ([eval.py:734](app/eval.py#L734)),
so parse failures are indistinguishable from real mediocre scores.

**Fix — four parts.**

**(a) A separate judge model.** In `tensorzero.toml`:

```toml
[models.judge_model]
routing = ["anthropic_judge"]

[models.judge_model.providers.anthropic_judge]
type = "anthropic"
model_name = "claude-sonnet-4-5"

[functions.judge]
type = "json"                 # structured output, no regex parsing

[functions.judge.variants.primary]
type = "chat_completion"
model = "judge_model"
temperature = 0.0
system_template = "templates/judge_system.minijinja"

[functions.critic]
type = "json"

[functions.critic.variants.primary]
type = "chat_completion"
model = "judge_model"
temperature = 0.0
```

Writer and judge now differ in both vendor and model. That is defensible in an interview.

**(b) Never fabricate a score.**

```python
def _parse_score(text: str) -> float | None:
    m = re.search(r"SCORE:\s*(\d+(?:\.\d+)?)\s*/\s*10", text, re.IGNORECASE)
    return round(float(m.group(1)) / 10.0, 2) if m else None
```

Drop `None` scores from the aggregate and emit a `judge_parse_failure_rate`
metric alongside them. Better: use TensorZero's `type = "json"` function above
and delete the regex entirely.

**(c) Fix the inverted metric.** `hallucination_risk` is scored 10 = worst while
every other metric is 10 = best, and nothing in the LangSmith dataset records the
direction. Either invert it to `groundedness` (10 = fully grounded) or tag each
metric with `"higher_is_better": bool` in the dataset metadata. Inverting is cleaner.

**(d) Build a real ground-truth set.** Create `evals/golden_set.jsonl` — 30 topics
you label by hand:

```jsonl
{"topic": "EU AI Act enforcement timeline", "must_mention": ["August 2026", "GPAI"], "must_not_mention": [], "human_quality": 4}
```

Add `evals/run_golden.py` that runs the graph over the set and reports:
- **Judge–human agreement**: Spearman correlation between `overall_quality` and
  your `human_quality` labels. Print the number. If it's below ~0.5, say so in the
  README — that is a far stronger signal than a dashboard full of 8.7/10s.
- **Citation support rate**: fraction of factual sentences carrying a `[n]`.
- **Coverage**: fraction of `must_mention` terms present.

**(e) Sample, don't judge everything.** [main.py:113](app/main.py#L113) runs four judge
calls on every single job. Make it configurable — `EVAL_SAMPLE_RATE=0.2` — and
run the full suite on the golden set instead.

**Acceptance criteria**
- README states the judge model differs from the writer model, and reports the
  measured judge–human correlation as a number.
- No score in LangSmith is ever a fabricated `0.5`.

**Effort:** ~5 hours (most of it labeling 30 topics).

---

## 1.4 Add tests, and gate the deploy on them

**Problem.** Zero tests. `.github/workflows/deploy.yml` builds and ships to
production. The rollback step can only ever fire on a container that fails to boot.

**Fix.** `tests/` with pytest + pytest-asyncio, everything external faked:

```
tests/
├── conftest.py              fakeredis fixture, stub TensorZero via respx, stub Tavily
├── test_cache.py            cosine threshold behaviour, hit/miss, key stability
├── test_agents.py           graph runs end-to-end against a stubbed gateway;
│                            critic rejection produces DIFFERENT queries on retry
├── test_memory.py           ltm_store idempotency; ltm_diff on two genuinely
│                            different reports returns a non-empty diff
├── test_eval.py             _parse_score returns None (not 0.5) on garbage
├── test_guardrails.py       GUARDRAIL_INTERVENED -> (False, reason)
├── test_queue.py            push -> consume -> ack round-trip on fakeredis
└── test_api.py              401 without key, 429 past the limit, /health degraded
                             when Redis is down
```

Add to `requirements-dev.txt`: `pytest`, `pytest-asyncio`, `fakeredis`, `respx`,
`ruff`, `mypy`.

Insert as the **first** job in the workflow, with build/deploy depending on it:

```yaml
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -r requirements.txt -r requirements-dev.txt
      - run: ruff check app/ pyrit_dashboard/
      - run: pytest -q --cov=app --cov-fail-under=60

  build-and-deploy:
    needs: test          # <-- deploy is now gated
    runs-on: ubuntu-latest
    ...
```

Also add `terraform fmt -check` and `terraform validate` to the same job.

**Acceptance criteria**
- `pytest` passes locally with no network access and no AWS credentials.
- A deliberately broken assertion blocks the deploy.
- Coverage badge or number in the README.

**Effort:** ~6 hours. **Highest ratio of interview credibility to effort here.**

---

## 1.5 Fix the correctness bugs

Each of these is small, and each is the kind of thing a reviewer finds by reading.

| # | File | Bug | Fix |
|---|---|---|---|
| a | [cache.py:1010](app/cache.py#L1010) | `_embed()` runs the transformer **synchronously inside async** `cache_get`, stalling the event loop for every request | Wrap in `asyncio.to_thread`, as [memory.py:516](app/memory.py#L516) already does correctly |
| b | [cache.py:1011](app/cache.py#L1011) | Cache lookup is O(N): `scan_iter` + one `GET` per key + numpy cosine in Python | Move the semantic cache into pgvector (you already run it) or use Redis 8 vector sets. One indexed query replaces N round-trips |
| c | [cache.py:1020](app/cache.py#L1020) | `abs(hash(query))` — Python salts string hashes per process, so replicas and restarts produce different keys | `hashlib.sha256(query.encode()).hexdigest()[:16]` |
| d | [main.py:34](app/main.py#L34) | Rate limit keys on `request.client.host`, which behind an ALB is the ALB. The limit is **global, not per-client** | Parse the leftmost untrusted hop of `X-Forwarded-For`; fall back to `client.host` only when the header is absent |
| e | [main.py:73,79,106](app/main.py#L73) | `ltm_store` runs on cache hit, LTM hit, *and* fresh generation, each with a new `uuid4()`, so `ON CONFLICT` never fires. Duplicate rows accumulate and `ltm_diff` compares a report to a byte-identical copy — **the diff feature is dead** | Store only on fresh generation. Make the PK a content hash of `(topic, report)` so re-generation is genuinely idempotent |
| f | cache.py + memory.py | Two `SentenceTransformer` instances loaded at import, ~90 MB duplicated per container | One shared `app/embeddings.py` module with a single lazily-initialised model |
| g | [guardrails.py:681](app/guardrails.py#L681) | `boto3.client()` constructed on every call | Module-level client, created once |
| h | [main.py:113](app/main.py#L113) | `asyncio.create_task(...)` with no reference held — the task can be GC'd mid-flight and its exceptions vanish | Keep a module-level `set()` of tasks with a `add_done_callback(discard)`, and log exceptions |
| i | [main.py:50](app/main.py#L50) | `except Exception: await asyncio.sleep(1)` in `_worker_loop` swallows everything silently. Broken Redis credentials spin forever while `/health` reports OK | Log the exception with traceback; increment a failure counter; mark the app degraded after N consecutive failures |
| j | [main.py:164](app/main.py#L164) | `/health` checks Redis only, so the ALB keeps routing to tasks with a dead DB pool or unreachable gateway | Check Redis + `SELECT 1` on Postgres + TensorZero `/health`; return 503 when any is down so the target group deregisters |
| k | [main.py:119](app/main.py#L119), [memory.py:525](app/memory.py#L525) | `datetime.utcnow()` is deprecated in 3.12 | `datetime.now(timezone.utc)` |

**Effort:** ~4 hours total.

---

# TIER 2 — Gets you to 8.5

## 2.1 Split the worker out of the API process

**Problem.** `_worker_loop` runs inside the FastAPI process
([main.py:138](app/main.py#L138)). Autoscaling targets CPU on a process that simultaneously
serves HTTP and runs 60-second LLM pipelines. You built a Redis Streams queue and
then didn't decouple anything — which is the one question an interviewer will ask
the moment they see Streams in the README.

**Fix.**
- `app/worker.py` with its own `__main__`, running only the consume loop.
- A third ECS service `research-agent-worker`, no load balancer, no public IP.
- API scales on ALB `RequestCountPerTarget`; worker scales on **Redis stream depth**
  (`XLEN` published to CloudWatch as a custom metric, target-tracking on it).
- Add a claim-check step: `XAUTOCLAIM` for messages pending longer than the job
  timeout, so a worker dying mid-job doesn't strand the message forever.
- Add a dead-letter stream after N delivery attempts.

This turns "I used a queue" into "I understand why you use a queue." It is the
strongest architecture talking point available to you and it costs one Terraform
resource block plus ~60 lines.

**Effort:** ~4 hours.

---

## 2.2 Close the security and infra gaps

| # | Where | Issue | Fix |
|---|---|---|---|
| a | [main.tf:783](terraform/main.tf#L783) | ECS tasks run in **public** subnets with `assign_public_ip = true`, making the private subnets and six VPC endpoints largely ornamental | Move tasks to `aws_subnet.private`, `assign_public_ip = false`. The endpoints you already pay for provide ECR/Secrets/Logs/Bedrock reachability. Only the ALB stays public |
| b | [main.tf:439](terraform/main.tf#L439) | RDS has no `storage_encrypted` | `storage_encrypted = true`, and set `performance_insights_enabled` while you're in there |
| c | [main.tf:420](terraform/main.tf#L420) | ElastiCache has no transit encryption, no at-rest encryption, no AUTH token | `transit_encryption_enabled = true`, `at_rest_encryption_enabled = true`, `auth_token` from `random_password`, stored in Secrets Manager. Update `REDIS_URL` to `rediss://` |
| d | [main.tf:452](terraform/main.tf#L452) | `deletion_protection = false` while the README claims it is enabled | Set it `true` and keep the README, or fix the README. **Do not ship a doc that contradicts the code** |
| e | [main.tf:490](terraform/main.tf#L490) | The PyRIT dashboard is exposed on port 8001 through the ALB with **no auth at all** — `/run-attacks` is world-callable and burns your OpenAI credits | Require the same `X-API-Key` on every PyRIT route, and restrict the 8001 listener rule to your own IP via an ALB listener condition. This is the most exploitable thing in the repo today |
| f | [main.py:145](app/main.py#L145) | `allow_origins=["*"]` alongside header auth | Restrict to the ALB origin |
| g | [deploy.yml:29](.github/workflows/deploy.yml#L29) | Long-lived `AWS_ACCESS_KEY_ID` / `SECRET` in GitHub secrets | GitHub OIDC + `aws-actions/configure-aws-credentials` with `role-to-assume`. Zero standing credentials. Two-line change, meaningful signal |
| h | [deploy.yml:8](.github/workflows/deploy.yml#L8) | Account ID `003861005384` hardcoded | Derive from `aws sts get-caller-identity` or a repo variable |
| i | [main.tf:504](terraform/main.tf#L504) | HTTPS optional; default deployment is plaintext, which is why `crypto.randomUUID()` broke | Provision an ACM cert and make HTTPS the documented default. Redirect 80 → 443 |
| j | [Dockerfile](app/Dockerfile) | Container runs as root, single-stage build carrying `gcc`/`libpq-dev` into the runtime image | Multi-stage build, `USER app`, and a `HEALTHCHECK` |

**Effort:** ~4 hours. Items (a), (c), (e), (g) are the ones that get noticed.

---

## 2.3 Make the red-team harness honest

**Problem.** [pyrit_dashboard/main.py:110-114](pyrit_dashboard/main.py#L110-L114) initialises
PyRIT for SQLite memory and then **bypasses it entirely** — attacks are 16 hardcoded
strings sent by `httpx`. Crescendo is "simulated" with fixed prompts because the
adversarial LLM isn't available. Worse,
[main.py:127-131](pyrit_dashboard/main.py#L127-L131):

```python
blocked = ("BLOCKED" in response_text or "ERROR" in response_text
           or "guardrail" in response_text.lower()
           or len(response_text) < 50)
```

Any short response, any network error, and any report that merely *mentions* the
word "guardrail" counts as a successful block. The metric is biased toward
"we're safe."

**Pick one and commit to it.**

**Option A — do it properly.** Wire the real `PromptSendingAttack` with an adversarial
model as the red-team LLM (Groq is cheap and already configured), so Crescendo
genuinely escalates across turns based on the target's replies. Persist to PyRIT
memory and read results back out of it rather than a Redis blob.

**Option B — drop the dependency and be accurate.** Remove `pyrit` from
`requirements.txt`, rename the service `redteam_harness`, and describe it in the
README as "a fixed-corpus prompt-injection harness with 16 attack prompts across 4
categories." Losing the buzzword costs you nothing; being caught claiming a
framework you bypassed costs you the interview.

**Either way, fix the classifier.** Determine blocked/passed from the HTTP status and
the structured `status` field, never from substring matching on report text:

```python
blocked = r1.status_code == 400 or data.get("status") == "blocked"
errored = data.get("status") == "error"   # a THIRD state — not a success
```

Report `blocked / passed / errored / timeout` as four distinct outcomes. An errored
attack is an inconclusive test, not a defended one.

**Effort:** ~2 hours for Option B, ~5 for Option A. Option B is the better
risk-adjusted choice.

---

## 2.4 Publish the numbers you'd be asked for

An interviewer will ask three questions. Have all three answered in the README.

**(a) What does one report cost?** Today: search + summarize + write + critic
(4 GPT-4o calls, 8 with a retry) plus 4 judge calls on *every* request. Instrument
it — TensorZero already returns token usage; log `input_tokens`, `output_tokens`
and computed cost per job, expose a rolling average on `/stats`. Then publish:

```
Cost per report: $0.0XX  (median, n=50)
p50 latency: XXs   p95: XXs
Cache hit rate: XX%   LTM hit rate: XX%
Idle infra cost: $XXX/mo
```

**(b) How well does it work?** Publish the golden-set results from 1.3, including
the judge–human correlation, **even if the correlation is mediocre**. Reporting a
weak number honestly reads as senior. A dashboard of 8.7/10s from a self-grading
judge reads as naive.

**(c) What are the failure modes?** A short "Known Limitations" section: search
provider coverage, model cutoff on non-indexed topics, single-region, worker restart
semantics. You already write reviews like this for your other projects — this repo
should have one too.

**Effort:** ~3 hours.

---

## 2.5 Rewrite the README to match the code

Current mismatches:

- Claims a **Search** agent — it retrieves nothing until 1.1 lands.
- Claims **PyRIT 0.14.0 automated red team** — PyRIT is initialised and bypassed.
- Claims RDS **deletion protection is enabled** — `main.tf` sets it `false`.
- Architecture table lists components without saying what is *not* there
  (no reranking, no HITL, single region, no multi-tenancy).

Add an **Architecture Decisions** section — 6–8 short entries with the trade-off you
actually made:

```markdown
### Why Redis Streams instead of SQS
Consumer groups give at-least-once delivery with per-consumer ack, and Redis was
already provisioned for the semantic cache and session memory. SQS would have meant
a second broker for no additional capability at this scale. Trade-off: no managed
DLQ, so the dead-letter stream is implemented in application code (app/worker.py).
```

Do the same for: TensorZero over LiteLLM, pgvector over a dedicated vector DB,
ECS Fargate over Lambda, Bedrock Guardrails over an in-process classifier,
`all-MiniLM-L6-v2` over a larger embedding model.

This is the cheapest item on the list and it changes how the whole repo reads.
The architecture diagram in `architecture.html` should be regenerated to show the
worker as a separate service and the search tool as an external dependency.

**Effort:** ~2 hours.

---

# TIER 3 — Beyond 8.5 (only after Tier 1 and 2 are done)

- **Streaming responses.** SSE token streaming from writer → frontend. Removes the
  poll loop and is what users actually expect from a research tool in 2026.
- **LangGraph checkpointer.** Postgres `AsyncPostgresSaver` so a worker crash resumes
  mid-graph instead of restarting the pipeline. You already run Postgres.
- **Human-in-the-loop.** LangGraph `interrupt()` before the writer node so a user can
  approve or edit the source set. Real use of a LangGraph feature that a for-loop
  cannot replicate — and a direct answer to "why LangGraph?"
- **Prompt versioning.** TensorZero variants with weights, A/B two writer prompts,
  and use the golden set to pick a winner on evidence.
- **Cost guardrail.** Per-session token budget in Redis; reject when exceeded.
- **Reranking.** FlashRank over search results before they hit the writer's context.

---

# Effort summary

| Tier | Items | Effort | Score |
|---|---|---|---|
| — | Current state | — | 6.5 |
| 1 | Search grounding, working critic loop, honest evals, tests, 11 bug fixes | ~21 h | ~7.8 |
| 2 | Worker split, security hardening, honest red team, published metrics, README | ~15 h | **8.5** |
| 3 | Streaming, checkpointing, HITL, prompt A/B | ~20 h | 9+ |

Roughly one focused week for 8.5.

---

# Definition of done for 8.5

- [ ] Every report cites real, resolvable URLs; removing the search key fails the job loudly
- [ ] Judge model ≠ writer model, and the README reports measured judge–human correlation
- [ ] `pytest` passes offline with no AWS credentials; CI blocks deploy on failure; coverage ≥ 60%
- [ ] All 11 bugs in 1.5 fixed, each with a regression test
- [ ] A rejected report demonstrably retries with *different* search queries (LangSmith trace)
- [ ] Worker runs as its own ECS service, scaling on queue depth
- [ ] ECS tasks in private subnets; RDS and Redis encrypted; Redis AUTH enabled
- [ ] PyRIT dashboard requires auth; blocked/passed/errored are three distinct outcomes
- [ ] CI authenticates via OIDC, no long-lived AWS keys
- [ ] README contains cost-per-report, p50/p95 latency, eval results, and known limitations
- [ ] Zero claims in the README that the code does not implement

---

## The one-line version

Items **1.1 (real search)**, **1.4 (tests)**, and **1.3 (independent judge)** are
worth more than everything else combined. They convert the project's three weakest
answers — *"where do the facts come from?"*, *"how do you know it works?"*,
*"what's your test story?"* — from stumbles into strengths. If you only have a
weekend, do those three.
