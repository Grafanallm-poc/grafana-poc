# grafana-poc

Grafana (13.2.0) + Prometheus, dashboards managed via Grafana Git Sync against this
repo. `.github/workflows/validate-dashboards.yml` checks every dashboard JSON on PR;
`CODEOWNERS` requires review before merge.

## Partner Onboarding Observability Agent

An LLM-powered agent (`agent/`) that turns a plain-English request like:

> Onboard DSP Moloco, track latency and 5XX, alert if p95 > 120ms.

into a standard monitoring dashboard, via:

1. **Spec extraction** — Gemini's structured JSON output (`response_json_schema`,
   via the free-tier Gemini API) turns the request into a typed `PartnerSpec`
   (`agent/schema.py`), using a versioned prompt (`agent/prompts/system_prompt.v1.md`).
   Defaults to `gemini-3.5-flash`; override with `GEMINI_MODEL`. The free tier returns
   intermittent `503`s under shared load — `spec_extractor.py` retries a few times with
   backoff, but if you see onboarding fail with a 503 after retries, it's Gemini
   capacity, not a bug; a paid key (or any paid LLM tier) would be far more reliable
   for a real demo.
2. **Template render** — `agent/dashboard_renderer.py` builds the standard SSP/DSP
   dashboard JSON (same panel set for every partner: QPS, latency, 2XX/5XX, timeouts,
   system load), in this repo's Git Sync dashboard format.
3. **Validation** — `agent/validator.py` checks the JSON is well-formed, required
   fields are present, queries are syntactically sane, and every metric referenced
   exists in the approved catalog (`agent/metric_catalog.yaml`) — rejecting anything
   that looks hallucinated.
4. **PR** — `agent/github_pr.py` opens a GitHub PR adding `dashboards/<partner>.json`.
   Existing CI validation + CODEOWNERS review gate the merge; Grafana's Git Sync
   deploys it automatically once merged.

### LLMOps

- Prompts are versioned files under `agent/prompts/` and go through PR review like code.
- `evals/testset.jsonl` is a set of sample onboarding requests with expected specs.
- `evals/run_eval.py` scores every prompt/model change against that test set for
  accuracy and hallucinated-metric rate.
- `.github/workflows/eval-gate.yml` runs that eval suite on any PR touching the
  prompts, extraction schema, or metric catalog, and fails the PR if quality regresses
  (`EVAL_MIN_SCORE`, default 0.8) or a hallucinated metric is detected.
- Langfuse traces every LLM call's cost/latency (`agent/observability.py`) — optional,
  skipped gracefully if `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` aren't set.
- The agent's own health and spend are visible in Grafana at
  `dashboards/agent-health.json` (request volume/latency, LLM token usage, cumulative
  LLM cost), scraped from `/metrics` on the agent service.

### Running it

```bash
cp .env.example .env   # fill in GEMINI_API_KEY, GITHUB_TOKEN, GITHUB_REPO at minimum
cd docker
docker compose up --build
```

- Grafana: http://localhost:3000
- Prometheus: http://localhost:9090
- Onboarding agent web form: http://localhost:8000

`GITHUB_TOKEN` needs `contents:write` and `pull_requests:write` on `GITHUB_REPO`.
`GRAFANA_PROMETHEUS_DS_UID` should match the UID of the Prometheus datasource you've
added in Grafana (Connections > Data sources > Prometheus).

### Running the eval gate locally

```bash
export GEMINI_API_KEY=...
python -m evals.run_eval
```
