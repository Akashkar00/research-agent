# Autonomous Research Agent

Give it a topic → it researches, writes a full report, safety-checks it, caches it, and remembers it. Built on AWS with a real multi-agent pipeline, red teaming, and LLM evaluation on every request.

---

## What It Uses

| Component | What It Does |
|---|---|
| **FastAPI** | REST API — receives topics, returns reports |
| **LangGraph** | 4-node pipeline: Search (real web search) → Summarize → Write → Verify (independent critic) |
| **Tavily** | Real web search — the *only* source of facts the pipeline is allowed to use. No key = jobs fail loudly, never silently fall back to model recall |
| **TensorZero** | LLM gateway — writer path routes to GPT-4o (falls back to Groq Llama); critic/judge path routes to Groq Llama on purpose, so grading is never done by the model being graded |
| **AWS Bedrock Guardrails** | Blocks harmful input and output automatically |
| **Redis (ElastiCache)** | Session memory + job queue |
| **PostgreSQL + pgvector (RDS)** | Long-term memory + semantic cache (both vector-indexed) |
| **LangSmith** | Traces every agent run + 5 LLM-as-judge metrics on a sample of reports (`EVAL_SAMPLE_RATE`) |
| **pytest** | 35+ tests against fakeredis/respx stubs, ~66% coverage — CI will not deploy if they fail |
| **PyRIT 0.14.0** | Automated red team attacks — jailbreak, XPIA, crescendo, skeleton key |
| **Terraform** | Creates all AWS infrastructure with one command |
| **GitHub Actions** | Runs the test suite, then builds Docker images and deploys to ECS on every push to `main` |

---

## File Structure

```
PROJECT/
├── app/
│   ├── main.py           API, background worker, all endpoints
│   ├── agents.py         LangGraph multi-agent graph (search, summarize, write, critic)
│   ├── tools/search.py   Tavily web search — the only source of facts
│   ├── cache.py          Semantic cache (pgvector)
│   ├── guardrails.py     Bedrock safety checks
│   ├── memory.py         Session memory (Redis) + long-term memory (pgvector)
│   ├── embeddings.py     Shared, lazily-loaded sentence-transformer model
│   ├── queue.py          Redis Streams job queue
│   ├── output.py         PDF export, JSON report, report diff
│   ├── eval.py           LangSmith LLM-as-judge evaluation (independent judge model)
│   ├── config.py         Loads everything from AWS Secrets Manager
│   ├── auth.py           API key middleware
│   ├── retry.py          Exponential backoff for LLM calls
│   ├── pool.py           PostgreSQL connection pool
│   └── Dockerfile
├── tests/                pytest suite — fakeredis/respx stubs, no network or AWS needed
├── evals/
│   ├── golden_set.jsonl  30 hand-picked topics for judge-human correlation
│   └── run_golden.py     Runs the pipeline over the golden set, reports the correlation
├── pyrit_dashboard/
│   ├── main.py           Red team attack dashboard (PyRIT 0.14.0)
│   ├── requirements.txt
│   └── Dockerfile
├── tensorzero/
│   ├── tensorzero.toml   LLM routing config with system prompts
│   └── Dockerfile
├── terraform/
│   └── main.tf           All AWS infrastructure
├── .github/workflows/
│   └── deploy.yml        Tests gate the build; CI/CD pipeline with rollback on failure
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

### 3. Create a GitHub repo and add secrets

1. Go to https://github.com and create a new repo named `research-agent`

2. Push this project to it:
```bash
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/YOUR_USERNAME/research-agent.git
git push -u origin main
```

3. Add these two secrets (repo → Settings → Secrets and variables → Actions → New repository secret):

| Secret Name | Where to get it |
|---|---|
| `AWS_ACCESS_KEY_ID` | Same key you used in Step 1 |
| `AWS_SECRET_ACCESS_KEY` | Same key you used in Step 1 |

---

### 4. Deploy all AWS infrastructure

```bash
cd terraform
terraform init
terraform apply -var="app_image=placeholder" -var="pyrit_image=placeholder"
```

Type `yes` when asked. Takes 5–10 minutes.

This creates: VPC, subnets, ECS cluster, ALB, ElastiCache Redis, RDS PostgreSQL, Bedrock Guardrail, Secrets Manager, ECR repos, IAM roles, VPC endpoints, auto-scaling, EventBridge weekly red team schedule.

After it finishes, note these outputs — you'll need them:
```
alb_dns        = "research-agent-alb-xxxxxxx.us-east-1.elb.amazonaws.com"
app_ecr_url    = "123456789.dkr.ecr.us-east-1.amazonaws.com/research-agent-app"
pyrit_ecr_url  = "123456789.dkr.ecr.us-east-1.amazonaws.com/research-agent-pyrit"
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

Wait for the workflow to turn green (~5–10 minutes). It:
1. Builds the app, PyRIT, and TensorZero Docker images
2. Pushes them to ECR
3. Registers new ECS task definitions
4. Updates both ECS services
5. Waits for stability — rolls back automatically if anything fails

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

**Redis and system stats:**
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

## PyRIT Red Team Dashboard

Open in browser:
```
http://<alb_dns>:8001/
```

This runs 4 types of attacks against your app to check if the guardrails are working:

| Attack | What it does |
|---|---|
| **Jailbreak** | Tries to bypass safety instructions directly |
| **XPIA** | Hides malicious instructions inside a research topic |
| **Crescendo** | Escalates from innocent questions toward harmful content step by step |
| **Skeleton Key** | Claims authority (researcher, CISO approval) to bypass restrictions |

Click **Run Selected Attacks** → wait 2–5 minutes → results appear showing BLOCKED or PASSED with a risk score.

Results are saved in Redis and survive container restarts.

**Run attacks via API:**
```bash
# All attack types
curl http://<alb_dns>:8001/run-attacks

# Specific types
curl "http://<alb_dns>:8001/run-attacks?types=jailbreak,xpia"

# Get results
curl http://<alb_dns>:8001/results
```

The weekly red team also runs automatically every Monday at 2am UTC via EventBridge.

---

## Tear Down Everything

```bash
cd terraform
terraform destroy -var="app_image=placeholder" -var="pyrit_image=placeholder"
```

#### Delete ECR Repo , S3 Bucket , Dynamo DB and Secret Manager

```bash
aws secretsmanager delete-secret --secret-id "research-agent/config" --force-delete-without-recovery --region us-east-1

```

Type `yes` when asked. This deletes all AWS resources — ECS, RDS, Redis, ALB, VPC, Bedrock Guardrail, Secrets Manager, ECR repos, everything.

> **Note:** RDS has deletion protection enabled. Terraform will remove it, but AWS will take a final snapshot first (named `research-agent-postgres-final-snapshot`). This is intentional so you don't lose data by accident.
