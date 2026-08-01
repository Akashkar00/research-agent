# Autonomous Research Agent

Give it a topic → it researches, writes a full report, safety-checks it, caches it, and remembers it. Built on AWS with a real multi-agent pipeline, red teaming, and LLM evaluation on every request.

---

## What It Uses

| Component | What It Does |
|---|---|
| **FastAPI** | REST API — receives topics, returns reports. Scales on ALB request count |
| **Worker (ECS)** | Separate service consuming the job queue — decoupled from the API so autoscaling targets the thing actually under load. Scales on Redis stream depth (`QueueDepth`, a custom CloudWatch metric it publishes itself) |
| **LangGraph** | 4-node pipeline: Search (real web search) → Summarize → Write → Verify (independent critic) |
| **Tavily** | Real web search — the *only* source of facts the pipeline is allowed to use. No key = jobs fail loudly, never silently fall back to model recall |
| **TensorZero** | LLM gateway — writer path routes to GPT-4o (falls back to Groq Llama); critic/judge path routes to Groq Llama on purpose, so grading is never done by the model being graded |
| **AWS Bedrock Guardrails** | Blocks harmful input and output automatically |
| **Redis (ElastiCache)** | Session memory + job queue, encrypted in transit/at rest with an AUTH token (`rediss://`) |
| **PostgreSQL + pgvector (RDS)** | Long-term memory + semantic cache (both vector-indexed), encrypted at rest, deletion protection on |
| **LangSmith** | Traces every agent run + 5 LLM-as-judge metrics on a sample of reports (`EVAL_SAMPLE_RATE`) |
| **pytest** | 40+ tests against fakeredis/respx stubs, ~70% coverage — CI will not deploy if they fail |
| **Red team harness** | A fixed-corpus prompt-injection harness — 16 attack prompts across 4 categories, API-key gated |
| **Terraform** | Creates all AWS infrastructure with one command |
| **GitHub Actions** | Runs the test suite, then builds Docker images and deploys to ECS on every push to `main` — authenticates via GitHub OIDC, no long-lived AWS keys stored anywhere |

---

## File Structure

```
PROJECT/
├── app/
│   ├── main.py           API process — HTTP endpoints only, no job processing
│   ├── worker.py         Standalone worker process (python -m app.worker) — consumes
│   │                     the queue, runs the pipeline, publishes its own scaling metric
│   ├── agents.py         LangGraph multi-agent graph (search, summarize, write, critic)
│   ├── tools/search.py   Tavily web search — the only source of facts
│   ├── cache.py          Semantic cache (pgvector)
│   ├── guardrails.py     Bedrock safety checks
│   ├── memory.py         Session memory (Redis) + long-term memory (pgvector)
│   ├── embeddings.py     Shared, lazily-loaded sentence-transformer model
│   ├── metrics.py        Per-job cost/latency/cache-hit tracking, read by /stats
│   ├── queue.py          Redis Streams job queue — claim-check (XAUTOCLAIM) + dead-letter
│   ├── output.py         PDF export, JSON report, report diff
│   ├── eval.py           LangSmith LLM-as-judge evaluation (independent judge model)
│   ├── config.py         Loads everything from AWS Secrets Manager
│   ├── auth.py           API key middleware
│   ├── retry.py          Exponential backoff for LLM calls
│   ├── pool.py           PostgreSQL connection pool
│   └── Dockerfile        Multi-stage, non-root, HEALTHCHECK
├── tests/                pytest suite — fakeredis/respx stubs, no network or AWS needed
├── evals/
│   ├── golden_set.jsonl  30 hand-picked topics for judge-human correlation
│   └── run_golden.py     Runs the pipeline over the golden set, reports the correlation
├── redteam_harness/
│   ├── main.py           Fixed-corpus prompt-injection harness (not PyRIT — see below)
│   ├── requirements.txt
│   └── Dockerfile
├── tensorzero/
│   ├── tensorzero.toml   LLM routing config with system prompts
│   └── Dockerfile
├── terraform/
│   └── main.tf           All AWS infrastructure
├── .github/workflows/
│   └── deploy.yml        Tests gate the build; OIDC auth; rollback on failure
├── bootstrap.bat         One-time backend setup (Windows)
├── bootstrap.sh          One-time backend setup (Mac/Linux)
├── requirements.txt      Python dependencies
├── requirements-dev.txt  Test/lint dependencies (pytest, ruff, fakeredis, respx)
├── index.html            Frontend UI
└── README.md
```

---

## Prerequisites

Install these before starting:

| Tool | Install | Check |
|---|---|---|
| AWS CLI | https://aws.amazon.com/cli/ | `aws --version` |
| Terraform | https://developer.hashicorp.com/terraform/install | `terraform --version` |
| Git | https://git-scm.com/downloads | `git --version` |

Docker is **not needed** on your machine. GitHub Actions builds and pushes images automatically.

---

## Setup — Follow in Order

### 1. Configure AWS credentials

```bash
aws configure
```

Enter:
- **AWS Access Key ID** — AWS Console → your name (top right) → Security Credentials → Create access key
- **AWS Secret Access Key** — shown once at creation, copy it immediately
- **Default region** — `us-east-1`
- **Default output format** — `json`

---

### 2. Create the Terraform backend (one time only)

Terraform needs an S3 bucket and DynamoDB table to store its state. Run the bootstrap script to create them:

**Windows:**
```cmd
bootstrap.bat
```

**Mac / Linux / Git Bash:**
```bash
chmod +x bootstrap.sh
./bootstrap.sh
```

Expected output:
```
S3 bucket  : research-agent-tfstate
DynamoDB   : research-agent-tf-locks
Bootstrap complete.
```

---

### 3. Create a GitHub repo

1. Go to https://github.com and create a new repo named `research-agent`

2. Push this project to it:
```bash
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/YOUR_USERNAME/research-agent.git
git push -u origin main
```

No AWS secrets to add here — CI authenticates via GitHub OIDC (see Step 4), not stored keys.
This first push will trigger a workflow run that fails at the deploy step (the OIDC role
doesn't exist until Terraform creates it) — that's expected, continue to Step 4.

---

### 4. Deploy all AWS infrastructure

```bash
cd terraform
terraform init
terraform apply -var="app_image=placeholder" -var="pyrit_image=placeholder" \
  -var="github_repo=YOUR_USERNAME/research-agent"
```

Type `yes` when asked. Takes 15–25 minutes — RDS and ElastiCache (both encrypted) are the
slow parts.

This creates: VPC, subnets, ECS cluster (app + worker + red-team harness services), ALB,
ElastiCache Redis (encrypted, AUTH token), RDS PostgreSQL (encrypted, deletion protection),
Bedrock Guardrail, Secrets Manager, ECR repos, IAM roles, a GitHub OIDC provider + deploy
role, VPC endpoints, auto-scaling (ALB request count for the API, queue depth for the
worker), EventBridge weekly red team schedule.

After it finishes, note these outputs — you'll need them:
```
alb_dns                  = "research-agent-alb-xxxxxxx.us-east-1.elb.amazonaws.com"
app_ecr_url              = "123456789.dkr.ecr.us-east-1.amazonaws.com/research-agent-app"
pyrit_ecr_url            = "123456789.dkr.ecr.us-east-1.amazonaws.com/research-agent-pyrit"
github_actions_role_arn  = "arn:aws:iam::123456789:role/research-agent-github-actions"
```

Set the account ID as a GitHub Actions repo **variable** (not secret — it isn't sensitive,
and the workflow needs it to build the OIDC role ARN before it has any credentials):
```bash
gh variable set AWS_ACCOUNT_ID --body "$(aws sts get-caller-identity --query Account --output text)"
```

---

### 5. Get your API keys

You need four keys:

| Key | Where to get it |
|---|---|
| `OPENAI_API_KEY` | https://platform.openai.com/api-keys |
| `GROQ_API_KEY` | https://console.groq.com/keys |
| `LANGSMITH_API_KEY` | https://smith.langchain.com → Profile → API Keys → Create |
| `TAVILY_API_KEY` | https://tavily.com → free tier is 1,000 searches/month |

LangSmith is free. It traces every agent run and stores evaluation scores automatically — no extra setup needed after you add the key.

Tavily is required — it's the *only* source of facts in the pipeline. Without it, every research job fails with a clear error instead of silently falling back to the model's training-data recall.

---

### 6. Fill in Secrets Manager

Terraform already filled in Redis URL, database URL, Guardrail ID, and all tuning parameters. You only need to add your three API keys.

Go to: **AWS Console → Secrets Manager → `research-agent/config` → Retrieve secret value → Edit**

Replace the `REPLACE_ME` values:
```json
{
  "OPENAI_API_KEY":    "sk-...",
  "GROQ_API_KEY":      "gsk_...",
  "LANGSMITH_API_KEY": "ls__...",
  "TAVILY_API_KEY":    "tvly-..."
}
```

Save. Leave everything else as is.

**Optional — set an API key to protect your endpoints:**

Add this field to the same secret:
```json
"API_KEY": "any-string-you-choose"
```

If set, every request to the app must include the header `X-API-Key: your-string`. The frontend has a field to enter it (saved in your browser). If left empty, the app runs without auth.

---

### 7. Wait for GitHub Actions to deploy

The `git push` in Step 3 already triggered the first deployment. Go check:

**GitHub repo → Actions tab**

Re-trigger it now that the OIDC role exists (Actions tab → this workflow → Run workflow,
or just push again). Wait for it to turn green (~10–15 minutes, mostly the app image
build). It:
1. Runs the test suite and `terraform fmt`/`validate` — blocks the rest of the job on failure
2. Builds the app, red-team harness, and TensorZero Docker images, pushes them to ECR
3. Registers new ECS task definitions and updates the app, worker, and red-team harness services
4. Waits for stability — rolls back automatically if anything fails

Once green, your app is live at the ALB URL from Step 4.

---

## Using the App

### Frontend

Open in browser:
```
http://<alb_dns>/
```

1. Enter your API key (if you set one in Step 6) — it saves in your browser
2. Type a research topic
3. Choose output format (text / PDF / JSON)
4. Click **Start Research** — polls automatically until done
5. Click **Show Changes vs Previous** to see what changed since last report on that topic

---

### API Endpoints

All requests need the header `X-API-Key: your-key` if you set one.

**Submit a research job:**
```bash
curl -X POST http://<alb_dns>/research \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-key" \
  -d '{"topic": "AI chip market 2025", "session_id": "abc123", "output_format": "text"}'
```
Returns: `{"job_id": "...", "session_id": "..."}`

**Poll for result:**
```bash
curl http://<alb_dns>/result/<job_id> -H "X-API-Key: your-key"
```
Returns `{"status": "pending"}` until done, then the full report.

**Download as PDF:**
```bash
curl http://<alb_dns>/result/<job_id>/pdf -H "X-API-Key: your-key" -o report.pdf
```

**Get session history:**
```bash
curl http://<alb_dns>/session/<session_id> -H "X-API-Key: your-key"
```

**Get report diff (what changed vs previous):**
```bash
curl http://<alb_dns>/diff/<topic> -H "X-API-Key: your-key"
```

**Redis stats + published cost/latency metrics** (see "What This Actually Costs" below):
```bash
curl http://<alb_dns>/stats -H "X-API-Key: your-key"
```

**Health check (no auth needed):**
```bash
curl http://<alb_dns>/health
```

---

## LangSmith — Traces and Evaluation

Every research job automatically:
1. Traces every agent node (search, summarize, write, verify) to LangSmith
2. On a sample of requests (`EVAL_SAMPLE_RATE`, default 1.0), runs 5 LLM-as-judge metrics:
   relevance, completeness, groundedness, overall quality, and citation support rate
3. Saves scores to a LangSmith dataset called `research-agent-reports`

**The judge is not the writer.** The report is written by GPT-4o (OpenAI); every judge and the
critic run on Groq's Llama-3.1-8b-instant instead — a different vendor and a different model,
so the model grading a report never grades its own work. See `tensorzero/tensorzero.toml`
(`judge_model`) and `app/eval.py`.

A parse failure never becomes a fabricated score: `_parse_score` returns `None`, aggregates
drop it, and `judge_parse_failure_rate` is reported alongside the real scores.

View traces: https://smith.langchain.com → Project: `research-agent`

### Golden set — measured judge-human agreement

`evals/golden_set.jsonl` ships with 30 hand-picked topics (`must_mention` terms + a `human_quality`
field you fill in by hand after reading the generated reports — it ships as `null`; a
self-labeled "human" score would defeat the point). Run it with:

```bash
python -m evals.run_golden
```

It reports citation support rate, `must_mention` coverage, and — once you've labeled at least
5-10 entries — the Spearman correlation between the judge's `overall_quality` score and your
human labels. **Report that number in this README even if it's weak** — a low, honestly-reported
correlation is a stronger signal than a dashboard of unverified 8.7/10s.

**Trigger batch evaluation manually** (runs the agent on recent user topics from the DB):
```bash
curl -X POST http://<alb_dns>/run-evaluation \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-key" \
  -d '{}'
```

Pass specific topics instead:
```bash
curl -X POST http://<alb_dns>/run-evaluation \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-key" \
  -d '{"topics": ["quantum computing", "AI regulations"]}'
```

---

## Red Team Harness

Open in browser:
```
http://<alb_dns>:8001/
```

**This is not PyRIT.** An earlier version initialized the PyRIT framework and then bypassed
it entirely — attacks were 16 hardcoded strings sent via plain `httpx`, and "Crescendo" was
simulated with fixed prompts because no adversarial LLM was wired up. Rather than ship that
under PyRIT's name, the `pyrit` dependency was dropped and this is described accurately: **a
fixed-corpus prompt-injection harness, 16 attack prompts across 4 categories**, sent directly
against the live API and classified from its real HTTP status and `status` field — never by
substring-matching the response text (an earlier version treated any short response, network
error, or report merely *mentioning* the word "guardrail" as a successful block, which biased
the metric toward "we're safe").

| Attack | What it does |
|---|---|
| **Jailbreak** | Tries to bypass safety instructions directly |
| **XPIA** | Hides malicious instructions inside a research topic |
| **Crescendo** | An ordered sequence of escalating prompts (not PyRIT's adaptive, LLM-driven Crescendo) |
| **Skeleton Key** | Claims authority (researcher, CISO approval) to bypass restrictions |

Each attempt is classified as one of four **distinct** outcomes — `blocked`, `passed`,
`errored`, or `timeout` — because an errored or timed-out attempt is inconclusive, not a
defended block, and must not be scored as either.

Requires the same `X-API-Key` as the main app if you set one (Step 6) — enter it in the
dashboard's API Key field, saved in your browser.

Click **Run Selected Attacks** → wait 2–5 minutes → results appear with an outcome and risk score.
Results are saved in Redis and survive container restarts.

**Run attacks via API:**
```bash
curl http://<alb_dns>:8001/run-attacks -H "X-API-Key: your-key"                      # all
curl "http://<alb_dns>:8001/run-attacks?types=jailbreak,xpia" -H "X-API-Key: your-key"  # specific
curl http://<alb_dns>:8001/results -H "X-API-Key: your-key"
```

The weekly run also happens automatically every Monday at 2am UTC via EventBridge.

---

## What This Actually Costs (and How Well It Works)

`/stats` publishes real numbers from live traffic (`job_metrics`), not static claims —
check it yourself against your own usage:

```bash
curl http://<alb_dns>/stats -H "X-API-Key: your-key"
```

- **Cost per report**: estimated from TensorZero's own token usage per call (`app/metrics.py`),
  blended at GPT-4o rates for the writer path — a ballpark, not a billing-accurate figure,
  since the actual provider can vary with fallback routing.
- **p50 / p95 latency**: end-to-end job time, computed over the last 200 jobs.
- **Cache hit rate / LTM hit rate**: fraction of jobs served from the semantic cache or
  long-term memory instead of running the full multi-agent pipeline.

Run `python -m evals.run_golden` for the golden-set numbers (citation support, `must_mention`
coverage, and — once you've hand-labeled `evals/golden_set.jsonl` — the judge-human Spearman
correlation). Report that correlation here once you have it, even if it's mediocre.

---

## Architecture Decisions

**Redis Streams instead of SQS.** Consumer groups give at-least-once delivery with
per-consumer ack, and Redis was already provisioned for session memory. SQS would have meant
a second broker for no added capability at this scale. Trade-off: no managed DLQ, so the
dead-letter stream (`{stream_key}:dlq`) is implemented in application code (`app/queue.py`).

**TensorZero over LiteLLM.** Native fallback routing per model, and a single place to swap
the judge model to a different vendor from the writer without touching application code.

**pgvector over a dedicated vector DB.** Already running Postgres for long-term memory;
adding a second stateful service (Pinecone/Qdrant/etc.) for the semantic cache would have
been another thing to operate for a workload this size.

**ECS Fargate over Lambda.** The pipeline holds a long-lived TensorZero sidecar connection
and an asyncpg pool per task — awkward to keep warm across Lambda's cold starts, and the
job runtime (up to a couple of minutes with retries) sits close to Lambda's practical limits.

**Bedrock Guardrails over an in-process classifier.** Managed, versioned, and auditable
independently of the application code that could be the thing bypassing it.

**`all-MiniLM-L6-v2` over a larger embedding model.** 384 dimensions keeps the pgvector index
small and queries fast; topic-level semantic matching (cache/LTM lookups) doesn't need a
larger model's fidelity.

**Worker split from the API process.** The original design ran the queue consumer inside the
FastAPI process — a Redis Streams queue existed but nothing was actually decoupled by it.
`app/worker.py` is now its own ECS service: the API scales on ALB request count, the worker
scales on Redis stream depth (a custom CloudWatch metric it publishes itself), and a worker
crash mid-job no longer risks starving HTTP request handling on the same event loop.

**GitHub OIDC over static AWS keys.** No long-lived credentials stored in GitHub at all —
each workflow run assumes a role scoped to `repo:OWNER/REPO:ref:refs/heads/main`, so a fork's
PR can't assume it, and there's no key to rotate or leak.

---

## Known Limitations

- **Search coverage** is whatever Tavily indexes — very recent or niche topics may return
  thin results, which the critic will (correctly) flag as under-cited rather than let the
  writer backfill from training-data recall.
- **Single region, single AZ for the worker/app tasks.** No cross-region failover; an
  `us-east-1` outage takes the whole app down.
- **ECS tasks run in public subnets** (`assign_public_ip = true`). The six VPC endpoints
  route AWS-service traffic privately, but a NAT Gateway (not currently provisioned, to avoid
  the ~$32-35/month it costs) would be required to move the tasks into the private subnets
  those endpoints were built for.
- **No HTTPS.** `terraform apply` supports it via `acm_certificate_arn`, but issuing a real
  ACM certificate requires a domain name pointed at the ALB, which this deployment doesn't have.
  Plain HTTP only, via the ALB's own DNS name.
- **Worker restart semantics**: a worker killed mid-job leaves its message unacked; another
  worker reclaims it via `XAUTOCLAIM` once it's been idle past the timeout (see
  `claim_stale_jobs` in `app/queue.py`), and a job is dead-lettered after
  `DLQ_MAX_DELIVERIES` failed attempts rather than retried forever.
- **Cost estimates are approximate** — see the caveat in "What This Actually Costs" above.

---

## Tear Down Everything

```bash
cd terraform
terraform destroy -var="app_image=placeholder" -var="pyrit_image=placeholder" \
  -var="github_repo=YOUR_USERNAME/research-agent"
```

#### Delete ECR Repo , S3 Bucket , Dynamo DB and Secret Manager

```bash
aws secretsmanager delete-secret --secret-id "research-agent/config" --force-delete-without-recovery --region us-east-1

```

Type `yes` when asked. This deletes all AWS resources — ECS, RDS, Redis, ALB, VPC, Bedrock Guardrail, Secrets Manager, ECR repos, everything.

> **Note:** RDS has deletion protection enabled. Terraform will remove it, but AWS will take a final snapshot first (named `research-agent-postgres-final-snapshot`). This is intentional so you don't lose data by accident.
