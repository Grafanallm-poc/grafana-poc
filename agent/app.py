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

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from agent.dashboard_renderer import render_dashboard
from agent.github_pr import GitHubPRError, open_dashboard_pr
from agent.llm.spec_extractor import extract_spec
from agent.observability import ONBOARD_DURATION, ONBOARD_REQUESTS
from agent.validator import validate_dashboard

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent.app")

app = FastAPI(title="Partner Onboarding Observability Agent")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"result": None})


@app.post("/onboard", response_class=HTMLResponse)
def onboard(request: Request, request_text: str = Form(...)):
    start = time.monotonic()
    result = {"request_text": request_text, "ok": False, "errors": [], "spec": None, "pr_url": None}

    try:
        spec = extract_spec(request_text)
        result["spec"] = spec.model_dump()
    except Exception as exc:  # LLM / parsing failure
        logger.exception("Spec extraction failed")
        ONBOARD_REQUESTS.labels(result="spec_invalid").inc()
        result["errors"] = [f"Could not extract a structured spec: {exc}"]
        ONBOARD_DURATION.observe(time.monotonic() - start)
        return templates.TemplateResponse(request, "index.html", {"result": result})

    dashboard = render_dashboard(spec)
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
        )
    except GitHubPRError as exc:
        logger.exception("PR creation failed")
        ONBOARD_REQUESTS.labels(result="pr_failed").inc()
        result["errors"] = [f"Dashboard validated, but opening the PR failed: {exc}"]
        ONBOARD_DURATION.observe(time.monotonic() - start)
        return templates.TemplateResponse(request, "index.html", {"result": result})

    ONBOARD_REQUESTS.labels(result="success").inc()
    ONBOARD_DURATION.observe(time.monotonic() - start)
    result["ok"] = True
    result["pr_url"] = pr["pr_url"]
    return templates.TemplateResponse(request, "index.html", {"result": result})


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
