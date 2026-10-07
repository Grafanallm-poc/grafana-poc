"""Web form front-end for the Partner Onboarding Observability Agent.

Pipeline: plain-English request -> LLM structured spec -> standard SSP/DSP
dashboard template -> validator -> GitHub PR. Every step's outcome is shown back
to the user; the agent's own request volume, latency and LLM cost are exposed at
/metrics for Prometheus (see docker/prometheus/prometheus.yml and
dashboards/agent-health.json).
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import os

from fastapi import BackgroundTasks, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

from agent import pr_outcomes
from agent.dashboard_renderer import render_dashboard
from agent.github_pr import GitHubPRError, open_dashboard_pr
from agent.lineage import build_lineage, stamp_dashboard
from agent.llm.spec_extractor import MODEL_CONFIG, extract_spec_with_lineage
from agent.observability import ONBOARD_DURATION, ONBOARD_REQUESTS, WEBHOOK_EVENTS
from agent.validator import validate_dashboard, validate_request_text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent.app")

app = FastAPI(title="Partner Onboarding Observability Agent")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

try:
    REGISTRY.register(pr_outcomes.AgentPRCollector())
except ValueError:  # already registered (module reloaded, e.g. in tests)
    pass


@app.on_event("startup")
def _startup() -> None:
    logger.info(
        "LLM models: primary=%s fallbacks=%s", MODEL_CONFIG["primary"], MODEL_CONFIG["fallbacks"]
    )
    if not os.getenv("GITHUB_WEBHOOK_SECRET"):
        logger.warning("GITHUB_WEBHOOK_SECRET not set; /webhooks/github will reject all deliveries.")
    pr_outcomes.start_cost_refresher()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"result": None})


@app.post("/onboard", response_class=HTMLResponse)
def onboard(request: Request, request_text: str = Form(...)):
    start = time.monotonic()
    result = {
        "request_text": request_text, "ok": False, "errors": [], "spec": None, "pr_url": None,
        "lineage": None,
    }

    guardrail = validate_request_text(request_text)
    if not guardrail.ok:
        ONBOARD_REQUESTS.labels(result="request_invalid").inc()
        result["errors"] = guardrail.errors
        ONBOARD_DURATION.observe(time.monotonic() - start)
        return templates.TemplateResponse(request, "index.html", {"result": result})

    try:
        extraction = extract_spec_with_lineage(request_text)
        spec = extraction.spec
        result["spec"] = spec.model_dump()
    except Exception as exc:  # LLM / parsing failure
        logger.exception("Spec extraction failed")
        ONBOARD_REQUESTS.labels(result="spec_invalid").inc()
        result["errors"] = [f"Could not extract a structured spec: {exc}"]
        ONBOARD_DURATION.observe(time.monotonic() - start)
        return templates.TemplateResponse(request, "index.html", {"result": result})

    lineage = build_lineage(
        request_text=request_text,
        extraction=extraction,
        slug=spec.slug(),
        dashboard_path=f"dashboards/{spec.slug()}.json",
    )
    result["lineage"] = lineage
    dashboard = stamp_dashboard(render_dashboard(spec), lineage)
    validation = validate_dashboard(dashboard)
    if not validation.ok:
        ONBOARD_REQUESTS.labels(result="dashboard_invalid").inc()
        result["errors"] = validation.errors
        ONBOARD_DURATION.observe(time.monotonic() - start)
        return templates.TemplateResponse(request, "index.html", {"result": result})

    try:
        pr = open_dashboard_pr(
            slug=spec.slug(),
            partner_name=spec.partner_name,
            dashboard_json=json.dumps(dashboard, indent=2),
            request_text=request_text,
            lineage=lineage,
        )
    except GitHubPRError as exc:
        logger.exception("PR creation failed")
        ONBOARD_REQUESTS.labels(result="pr_failed").inc()
        result["errors"] = [f"Dashboard validated, but opening the PR failed: {exc}"]
        ONBOARD_DURATION.observe(time.monotonic() - start)
        return templates.TemplateResponse(request, "index.html", {"result": result})

    try:
        pr_outcomes.record_opened(pr_number=pr["number"], pr_url=pr["pr_url"], lineage=lineage)
    except Exception:  # never fail the user's request over bookkeeping
        logger.exception("Could not record PR in the outcome store")

    ONBOARD_REQUESTS.labels(result="success").inc()
    ONBOARD_DURATION.observe(time.monotonic() - start)
    result["ok"] = True
    result["pr_url"] = pr["pr_url"]
    return templates.TemplateResponse(request, "index.html", {"result": result})


@app.post("/webhooks/github")
async def github_webhook(
    request: Request,
    background: BackgroundTasks,
    x_github_event: str = Header(default=""),
    x_hub_signature_256: str | None = Header(default=None),
):
    """GitHub webhook receiver (content type application/json, events: Pull requests).
    Verifies the HMAC signature, then processes in the background so GitHub's
    10-second delivery timeout is never hit by the follow-up API calls."""
    body = await request.body()
    if not pr_outcomes.verify_signature(os.getenv("GITHUB_WEBHOOK_SECRET", ""), body, x_hub_signature_256):
        WEBHOOK_EVENTS.labels(event=x_github_event or "unknown", result="bad_signature").inc()
        raise HTTPException(status_code=401, detail="invalid signature")

    if x_github_event == "ping":
        WEBHOOK_EVENTS.labels(event="ping", result="processed").inc()
        return {"ok": True, "pong": True}
    if x_github_event != "pull_request":
        WEBHOOK_EVENTS.labels(event=x_github_event or "unknown", result="ignored").inc()
        return {"ok": True, "ignored": x_github_event}

    payload = json.loads(body)
    action = payload.get("action", "")
    if action not in ("opened", "closed", "reopened", "edited"):
        WEBHOOK_EVENTS.labels(event="pull_request", result="ignored").inc()
        return {"ok": True, "ignored": action}

    def process() -> None:
        try:
            status = pr_outcomes.handle_pull_request(payload["pull_request"], action)
            logger.info("PR #%s %s: %s", payload["pull_request"]["number"], action, status)
            WEBHOOK_EVENTS.labels(
                event="pull_request", result="ignored" if status.startswith("ignored") else "processed"
            ).inc()
        except Exception:
            logger.exception("Webhook processing failed")
            WEBHOOK_EVENTS.labels(event="pull_request", result="error").inc()

    background.add_task(process)
    return JSONResponse({"ok": True, "queued": action}, status_code=202)


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
