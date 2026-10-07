# grafana-poc

An observability stack (Grafana + Prometheus) plus a **Partner Onboarding Observability
Agent**: an LLM-powered service that turns a plain-English request like

> Onboard DSP Moloco, track latency and 5XX, alert if p95 > 120ms.

into a standard monitoring dashboard — reviewed via a GitHub PR, deployed automatically
by Grafana Git Sync once merged.

## Table of contents

- [Why this exists](#why-this-exists)
- [Architecture](#architecture)
- [Repository layout](#repository-layout)
- [Setup guide: from zero to a running stack](#setup-guide-from-zero-to-a-running-stack)
  1. [Provision a host](#1-provision-a-host)
  2. [Install Docker](#2-install-docker)
  3. [Clone the repo](#3-clone-the-repo)
  4. [Get a Gemini API key](#4-get-a-gemini-api-key)
  5. [Create a GitHub token](#5-create-a-github-token)
  6. [Configure `.env`](#6-configure-env)
  7. [Bring up the stack](#7-bring-up-the-stack)
  8. [Point Grafana's Git Sync at this repo](#8-point-grafanas-git-sync-at-this-repo)
  9. [Smoke-test everything](#9-smoke-test-everything)
- [Supported dashboard metrics](#supported-dashboard-metrics)
- [Guardrails](#guardrails)
- [LLMOps](#llmops)
  - [Lineage stamping](#lineage-stamping)
  - [Pinned models + fallback](#pinned-models--fallback)
  - [PR-outcome tracking + agent health](#pr-outcome-tracking--agent-health)
  - [Reviewer corrections become test cases](#reviewer-corrections-become-test-cases)
- [Day-2 operations](#day-2-operations)
- [Known limitations](#known-limitations)
- [Grafana Assistant — why not just use that?](#grafana-assistant--why-not-just-use-that)

---

## Why this exists

Every new SSP/DSP integration needs a dashboard tracking latency, 2XX/5XX, QPS,
timeouts, and system load. Normally only a technical person can build one, which
causes delays and inconsistent dashboards. This agent lets anyone request one in
plain English; an LLM extracts a structured spec, a fixed template renders the
dashboard, a validator rejects anything that looks wrong (bad JSON, hallucinated
metrics), and a human reviews the resulting PR before it deploys.

## Architecture

```
plain-English request
        │
        ▼
┌───────────────────┐   structured output   ┌──────────────────────┐
│  Gemini (LLM)      │ ────────────────────▶ │ PartnerSpec          │
│  agent/llm/        │   (JSON schema,       │ (pydantic model,     │
│  spec_extractor.py │    no free-form JSON) │  agent/schema.py)    │
└───────────────────┘                        └──────────┬───────────┘
                                                          │
                      the LLM's job ends here — everything below is
                      deterministic code, not AI
                                                          ▼
                                          ┌──────────────────────────┐
                                          │ dashboard_renderer.py     │
                                          │ fixed template per metric │
                                          └──────────┬────────────────┘
                                                      ▼
                                          ┌──────────────────────────┐
                                          │ validator.py               │
                                          │ JSON/schema/query/metric   │
                                          │ existence checks            │
                                          └──────────┬────────────────┘
                                                      ▼
                                          ┌──────────────────────────┐
                                          │ github_pr.py                │
                                          │ opens a PR adding           │
                                          │ dashboards/<partner>.json   │
                                          └──────────┬────────────────┘
                                                      ▼
                                   human reviews + merges (CODEOWNERS)
                                                      ▼
                                   Grafana Git Sync deploys automatically
```

Splitting "AI understands intent" from "code generates the dashboard" is deliberate:
the LLM never touches the actual Grafana JSON, which is what makes hallucination
detection, consistent panel layouts, and a CI eval gate on prompt changes possible.

## Repository layout

```
agent/                     the onboarding agent service (FastAPI)
  app.py                   web form + /onboard + /webhooks/github + /metrics
  schema.py                PartnerSpec / Metric / Alert pydantic models
  llm/
    spec_extractor.py      Gemini structured-output call, pinned model + fallback chain
    models.yaml            pinned primary model + ordered fallbacks (PR-reviewed)
  dashboard_renderer.py    PartnerSpec -> Grafana dashboard JSON (the "template")
  spec_inference.py        reverses the template: dashboard JSON -> the spec that
                           would have produced it (for reviewer-correction detection)
  lineage.py               stamps request/prompt/model/trace into the dashboard JSON
                           and PR body; encodes/parses the hidden lineage marker
  pr_outcomes.py           GitHub webhook handler + outcome classification
                           (merged_unchanged / merged_edited / closed_unmerged)
  store.py                 SQLite persistence for PR lineage + outcomes (agent_data volume)
  validator.py             JSON/schema/query/metric-existence checks
  github_pr.py             opens the GitHub PR (now lineage-stamped)
  observability.py         Prometheus metrics + optional Langfuse tracing
  metric_catalog.yaml      ground-truth list of metrics the template may reference
  prompts/                 versioned system prompts (system_prompt.v1.md, v2.md, ...)
  templates/index.html     the web form

dashboards/                every dashboard, hand-built or agent-generated — this is
                           exactly what Grafana's Git Sync watches
  agent-health.json        the agent's own self-monitoring + PR-outcome dashboard

evals/
  testset.jsonl            sample requests + expected structured specs
  run_eval.py              scores extraction accuracy + hallucination rate
  promote_candidate.py     promotes a reviewer-correction candidate into testset.jsonl
  candidates/              auto-opened PRs with unconfirmed reviewer corrections,
                           awaiting a human to promote or discard them

tools/dummy_exporter/      demo-only synthetic traffic generator (no real SSP/DSP
                           traffic exists in this POC, so this fakes it)

scripts/
  rotate-gemini-key.sh     rotates GEMINI_API_KEY across EC2 + GitHub secret
  backfill_pr_outcomes.py  reconstructs outcomes for PRs closed before the webhook existed

tests/                     unit tests for lineage, PR outcomes, fallback, spec inference

docker/
  docker-compose.yaml      prometheus + grafana + agent (+ agent_data volume) + dummy-exporter
  prometheus/prometheus.yml
  grafana-provisioning/datasources/prometheus.yaml   auto-provisions the datasource

.github/workflows/
  validate-dashboards.yml  JSON-syntax-checks every dashboard on PR
  eval-gate.yml            runs evals/run_eval.py on prompt/schema/model PRs
  CODEOWNERS               requires review on dashboards/ changes
```

---

## Setup guide: from zero to a running stack

This walks through everything from an empty VM to a fully working deployment —
Grafana, Prometheus, Git Sync, and the onboarding agent.

### 1. Provision a host

Any Linux VM with a public IP works (this was built and tested on an Ubuntu EC2
instance). Open these ports in its firewall/security group — ideally restricted to
your own IP, not `0.0.0.0/0`:

| Port | Service |
|---|---|
| 3000 | Grafana |
| 8000 | Onboarding agent web form |
| 9090 | Prometheus (optional — only if you want to query it directly) |

### 2. Install Docker

```bash
sudo apt-get update && sudo apt-get install -y docker.io git
sudo systemctl enable --now docker
sudo usermod -aG docker $USER   # log out/in (or start a new shell) for this to apply
docker compose version          # confirm the compose plugin is present
```

### 3. Clone the repo

```bash
git clone git@github.com:Grafanallm-poc/grafana-poc.git
cd grafana-poc
```

### 4. Get a Gemini API key

1. Go to **aistudio.google.com** → **Get API key** → **Create API key**.
2. When prompted for a project, you can use an existing one or create a new one —
   note that Gemini's free-tier daily quota (20 requests/day as of writing) is scoped
   **per Google Cloud project, not per key**. A second key under the *same* project
   shares the same exhausted quota; only a genuinely new project gets a fresh pool.
3. Copy the key (starts with `AIzaSy...` or `AQ....` depending on when it was issued).

### 5. Create a GitHub token

The agent needs a token to open PRs, and (separately) CI needs the Gemini key as a
repo secret:

1. GitHub → Settings → Developer settings → Fine-grained tokens → generate one scoped
   to this repo with **Contents: Read & write** and **Pull requests: Read & write**.
2. Keep it handy for the next step and for `scripts/rotate-gemini-key.sh` later.

### 6. Configure `.env`

```bash
cp .env.example .env
```

Fill in at minimum:
- `GEMINI_API_KEY` — from step 4
- `GITHUB_TOKEN` — from step 5
- `GITHUB_REPO` — `owner/repo` for your fork/copy of this repo

Leave `GRAFANA_PROMETHEUS_DS_UID=prometheus` as-is — the committed provisioning file
(`docker/grafana-provisioning/datasources/prometheus.yaml`) creates the datasource
with exactly that UID automatically, so no manual Grafana UI step is needed for this.

### 7. Bring up the stack

```bash
cd docker
docker compose up --build -d
docker compose logs -f agent   # watch for startup errors, Ctrl-C when happy
```

This starts four containers: `prometheus`, `grafana`, `onboarding-agent`, and
`dummy-exporter` (synthetic demo data — see [Known limitations](#known-limitations)
for why it exists and how to remove it once real partner metrics exist).

Open `http://<host-ip>:3000`, log in (default `admin`/`admin`), and **change the
password immediately**.

### 8. Point Grafana's Git Sync at this repo

This is the one step that still requires clicking through Grafana's UI (it wasn't
automated here). In Grafana 12+/13, this lives under **Administration → Provisioning**
(naming varies by exact version — if you don't see it, check Grafana's own Git Sync /
"dashboards as code" docs for your version):

1. Add a new Git repository connection pointing at this repo (`main` branch, path
   `dashboards/`).
2. Authenticate it with a GitHub token/App that has at least read access to this repo.
3. Set the sync interval (this POC uses the default ~60s poll).

Once configured, merging a PR that adds/changes a file under `dashboards/` will show
up in Grafana automatically within one sync interval — no manual import needed.

### 9. Smoke-test everything

```bash
# Agent is up
curl http://<host-ip>:8000/healthz

# Prometheus is scraping all three targets
curl -s http://<host-ip>:9090/api/v1/targets | python3 -m json.tool
```

Then open `http://<host-ip>:8000` and submit a request like:

> Onboard DSP Moloco, track latency and 5XX, alert if p95 > 120ms.

It should validate, open a real PR on GitHub, and — once you merge that PR — the
dashboard should appear in Grafana with live (synthetic) data within a minute or two.

---

## Supported dashboard metrics

The LLM can only select from this fixed list (anything else gets mapped to the
closest match or dropped — see `agent/prompts/system_prompt.v2.md`); the renderer
further restricts business metrics to the matching partner type.

**Infra/integration layer — valid for both SSP and DSP:**
QPS · Latency (p50/p95/p99) · 2XX Success Rate · 5XX Error Rate · Timeouts · System Load

**DSP-only business layer:**
Bid Rate · Win Rate · No-Bid Rate (+ reason breakdown) · Timeout-to-Bid Ratio · eCPM ·
Spend vs Daily Budget

**SSP-only business layer:**
Fill Rate · Revenue RPM · Viewability Rate · Render Rate ·
Demand-Partner Breakdown (bid rate / timeout rate / latency, sliced per DSP — 3 panels) ·
Blocked Creatives by Reason

If no metrics are mentioned at all, the request defaults to just the infra set —
business metrics are opt-in only, by explicit request.

**Current limitation:** the agent only *creates* new partner dashboards; asking it to
modify an existing one (e.g. "also track viewability for Adcolony") will fail when
opening the PR, since it doesn't fetch the file's current Git `sha` or merge with the
existing panel set. See the code comments in `agent/github_pr.py` if extending this.

---

## Guardrails

- **Input guardrail** — every request must explicitly state SSP or DSP and name at
  least one metric (or say "standard monitoring"); rejected before it reaches the
  LLM, saving quota on requests that would fail anyway (`validate_request_text` in
  `agent/validator.py`).
- **Output guardrail** — the generated dashboard must be valid JSON with required
  fields, syntactically sound queries, and every referenced metric present in the
  approved catalog; anything that looks hallucinated is rejected before a PR opens
  (`validate_dashboard` in `agent/validator.py`).
- **Type guardrail** — DSP-only metrics (bid rate, win rate, etc.) can never be
  applied to an SSP partner, and vice versa, enforced in the template regardless of
  what the LLM extracts (`agent/dashboard_renderer.py`).
- **Quality guardrail** — a CI eval gate blocks any prompt or model change that
  lowers extraction accuracy or introduces hallucinated metrics, before it reaches
  production (see [LLMOps](#llmops) below).

---

## LLMOps

- **Prompts are versioned** under `agent/prompts/` (`system_prompt.v1.md`, `v2.md`,
  ...) and reviewed via the same PR process as code — never edit history in place,
  bump the version instead.
- **`evals/testset.jsonl`** is a set of sample requests with expected structured
  specs; **`evals/run_eval.py`** scores every prompt/schema change against it for
  extraction accuracy and hallucinated-metric rate.
- **`.github/workflows/eval-gate.yml`** runs that suite on any PR touching prompts,
  the extraction schema, or the metric catalog, failing the PR if quality regresses
  (`EVAL_MIN_SCORE`, default 0.8) or a hallucinated metric is detected. Requires the
  `GEMINI_API_KEY` repo secret (see step 5/6 above, or `scripts/rotate-gemini-key.sh`).
- **Langfuse** (optional) traces every LLM call's cost/latency —
  `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` in `.env`; skipped gracefully if unset.
- **The agent's own health** (request volume/latency, LLM token usage, cumulative
  cost) is visible in Grafana via `dashboards/agent-health.json`, scraped from the
  agent's own `/metrics` endpoint.

Run the eval suite locally:
```bash
export GEMINI_API_KEY=...
python -m evals.run_eval
```

### Lineage stamping

Every generated dashboard records where it came from — the original request, prompt
version, model actually used (incl. fallback), Langfuse trace ID and agent version:

- **Dashboard JSON** — `spec.description` carries the request/prompt/model/trace,
  `spec.tags` gets `agent-generated`, `prompt:<v>`, `model:<id>`, and `spec.links` gets
  a **"Langfuse trace …"** link: open the dashboard in Grafana, click it, land on the
  exact LLM call.
- **PR description** — a lineage table, an editable `agent-spec` JSON block, and a
  hidden `<!-- agent-lineage:v1 … -->` marker that outcome tracking parses back.

Set `LANGFUSE_PROJECT_ID` to build trace links without an API lookup (otherwise the
SDK looks it up once). Code: `agent/lineage.py`.

### Pinned models + fallback

`agent/llm/models.yaml` pins the exact primary model and an ordered fallback list.
Because it lives under `agent/llm/`, changing it triggers the eval gate. On
`429 RESOURCE_EXHAUSTED` the extractor falls straight through to the next model (no
pointless retries); on 5xx it retries the same model a few times first; 404 also
falls through. Each request's lineage records which model served it, and
`agent_llm_model_calls_total` / `agent_llm_fallback_total` show it in Grafana.
The eval gate scores **one model with fallback disabled** (default: the primary);
run `EVAL_MODEL=gemini-3.1-flash-lite python -m evals.run_eval` to gate the fallback too.

### PR-outcome tracking + agent health

A GitHub webhook (`POST /webhooks/github`, HMAC-verified with
`GITHUB_WEBHOOK_SECRET`) classifies every closed agent PR:

| Outcome | Meaning |
|---|---|
| `merged_unchanged` | merged exactly as generated — the most honest quality signal |
| `merged_edited` | a reviewer changed the dashboard JSON or the `agent-spec` block first |
| `closed_unmerged` | closed without merging |

Outcomes are kept in SQLite on the `agent_data` volume (survive restarts) and exposed
on `/metrics` as `agent_prs{outcome,prompt_version,model}`,
`agent_pr_time_to_merge_seconds` (histogram) and `agent_dashboard_cost_usd`
(summary; cost per trace pulled from Langfuse's API, falling back to the local token
estimate). `dashboards/agent-health.json` shows acceptance / edit / rejection rate,
time-to-merge, cost per dashboard, per-prompt-version outcomes, guardrail rejections,
extraction failures and model fallbacks.

**Webhook setup:** repo → Settings → Webhooks → Add webhook → Payload URL
`http://<host>:8000/webhooks/github`, content type `application/json`, secret =
`GITHUB_WEBHOOK_SECRET`, events: *Pull requests*. The security group must allow
GitHub's `hooks` IP ranges (`curl https://api.github.com/meta | jq .hooks`) on 8000.

**Backfill** PRs closed before the webhook existed (older PRs without a lineage
marker are reconstructed from their first commit):

```bash
docker exec onboarding-agent python -m scripts.backfill_pr_outcomes          # dry run
docker exec onboarding-agent python -m scripts.backfill_pr_outcomes --apply
```

### Reviewer corrections become test cases

When an agent PR is edited or closed, the webhook opens a PR (label
`eval-candidate`) adding `evals/candidates/<slug>-pr<N>.json` with the original
request, the extracted spec and the reviewer's corrected spec — taken from an edited
`agent-spec` block if there is one, otherwise inferred from the merged dashboard
(`agent/spec_inference.py` reverses the deterministic template). Layout-only edits
that don't change the spec are recorded but don't produce a candidate.

A person confirms the answer and promotes it:

```bash
python -m evals.promote_candidate --list
python -m evals.promote_candidate evals/candidates/moloco-pr42.json
```

That appends it to `evals/testset.jsonl`; the eval gate then runs it on every
prompt/model change. See `evals/candidates/README.md`.

Unit tests for all of the above: `python -m pytest -q tests`.

---

## Day-2 operations

### Rotating `GEMINI_API_KEY`

`scripts/rotate-gemini-key.sh` updates the key everywhere it's used — the live
agent's `.env` (recreating the container, since `docker restart` does **not** reload
`--env-file`, only a fresh `docker run`/recreate does) and the GitHub Actions secret:

```bash
cp .env.local.example .env.local   # fill in EC2_SSH_KEY_PATH + GITHUB_TOKEN once;
                                    # gitignored, auto-sourced by the script
./scripts/rotate-gemini-key.sh     # prompts for the new key (hidden input)
```

### Adding synthetic demo data for a new partner

`tools/dummy_exporter/exporter.py` only fakes traffic for the partner slugs listed in
`SSP_PARTNERS`/`DSP_PARTNERS` (or the `DUMMY_SSP_PARTNERS`/`DUMMY_DSP_PARTNERS` env
vars). Add a new slug there, rebuild, and redeploy that one container — no other
component needs to change.

### Common issues

| Symptom | Cause | Fix |
|---|---|---|
| Dashboard panels all show "No data" | No Prometheus datasource configured in Grafana | Confirm `docker/grafana-provisioning/datasources/prometheus.yaml` is mounted (step 6); check via the Grafana UI under Connections → Data sources |
| Onboarding fails with `503 UNAVAILABLE` | Gemini free-tier capacity, not a bug | Retried automatically a few times; if it keeps happening, try a less-popular model via `GEMINI_MODEL`, or get a paid key |
| Onboarding fails with `429 RESOURCE_EXHAUSTED` | Daily free-tier quota exhausted (shared per Google Cloud **project**) | Wait for the daily reset, or enable billing on that project — a new key under the *same* project will not help |
| Key rotation "works" but requests still fail with `API_KEY_INVALID` | Pasted value got corrupted (stale clipboard, hidden-prompt paste issue) | Re-run `rotate-gemini-key.sh`, verify the new value's length/prefix before trusting it |
| Updated `.env` but the agent still uses the old value | `docker restart` doesn't reload `--env-file` | Recreate the container (`docker rm -f onboarding-agent && docker run ...`, or `docker compose up -d --force-recreate agent`) |

---

## Known limitations

- **Gemini free-tier quota is tight** (20 requests/day as of writing, shared per
  Google Cloud project) — fine for a demo, not for real usage without enabling
  billing.
- **Create-only, not update-in-place** — see [above](#supported-dashboard-metrics).
- **Standalone UI, not embedded in Grafana** — the web form runs as a separate app on
  its own port, not as a Grafana plugin inside Grafana's own UI. Embedding it there
  would be a real (and separate) chunk of work.
- **Synthetic demo data** — `tools/dummy_exporter` exists only because no real
  SSP/DSP integration sends metrics in this POC. Remove it once real partner traffic
  exists; the dashboard queries themselves don't change.

## Grafana Assistant — why not just use that?

Grafana Assistant is a conversational helper built into Grafana's own UI, for
exploring/querying/troubleshooting *existing* dashboards. It has no equivalent of
this repo's governed pipeline: no structured-spec extraction, no templated
generation, no metric-existence validation, no GitOps PR flow, and no LLMOps layer
(prompt versioning, eval gate, cost tracing). Its deeper AI features are also largely
Cloud-tier gated, which doesn't fit an OSS + GitHub/GitLab setup. The two look similar
on the surface — both take a natural-language request — but the architecture and
guarantees underneath are different by design.
