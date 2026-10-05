"""Validates a generated dashboard before it's allowed into a PR:
1. JSON is well-formed.
2. Required dashboard fields are present.
3. Every PromQL query is syntactically sane (balanced braces/parens, non-empty).
4. Every metric referenced actually exists in the approved catalog
   (agent/metric_catalog.yaml) — this is the "metrics actually exist" check from
   the brief. In this POC the catalog stands in for a live metrics-metadata API;
   swapping in a Prometheus /api/v1/label/__name__/values lookup is a drop-in
   replacement for check_metrics_exist().
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_CATALOG_PATH = Path(__file__).resolve().parent / "metric_catalog.yaml"
# Only matches an identifier immediately preceding `{` or `[` — i.e. an actual metric
# reference (`partner_requests_total{...}`), not a label name inside a `by (...)` /
# `without (...)` grouping clause (e.g. the "reason" in `sum by (reason) (...)`), which
# is followed by `)`, not `{`/`[`, and would otherwise look like a hallucinated metric.
_METRIC_NAME_RE = re.compile(r"\b([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(?:\{|\[)")


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)


def _load_catalog_metric_names() -> set[str]:
    catalog = yaml.safe_load(_CATALOG_PATH.read_text())
    return {name for entry in catalog["metrics"].values() for name in entry["promql_metrics"]}


def check_json_valid(dashboard_json: str) -> ValidationResult:
    try:
        json.loads(dashboard_json)
    except json.JSONDecodeError as exc:
        return ValidationResult(False, [f"Invalid JSON: {exc}"])
    return ValidationResult(True)


def check_required_fields(dashboard: dict) -> ValidationResult:
    errors = []
    for required in ("apiVersion", "kind", "metadata", "spec"):
        if required not in dashboard:
            errors.append(f"Missing required top-level field: {required}")
    if errors:
        return ValidationResult(False, errors)

    if "name" not in dashboard["metadata"]:
        errors.append("Missing required field: metadata.name")

    spec = dashboard["spec"]
    for required in ("title", "panels", "schemaVersion"):
        if required not in spec:
            errors.append(f"Missing required spec field: {required}")
    if "panels" in spec and not spec["panels"]:
        errors.append("Dashboard has no panels")
    return ValidationResult(not errors, errors)


def _iter_exprs(dashboard: dict):
    for panel in dashboard.get("spec", {}).get("panels", []):
        for target in panel.get("targets", []):
            yield panel.get("title", "<untitled panel>"), target.get("expr", "")


def check_queries_well_formed(dashboard: dict) -> ValidationResult:
    errors = []
    for panel_title, expr in _iter_exprs(dashboard):
        if not expr.strip():
            errors.append(f"Panel '{panel_title}': empty query")
            continue
        if expr.count("(") != expr.count(")"):
            errors.append(f"Panel '{panel_title}': unbalanced parentheses in '{expr}'")
        if expr.count("{") != expr.count("}"):
            errors.append(f"Panel '{panel_title}': unbalanced braces in '{expr}'")
    return ValidationResult(not errors, errors)


def _extract_candidate_metric_names(expr: str) -> set[str]:
    reserved = {
        "sum", "rate", "histogram_quantile", "avg", "max", "min", "count", "by",
        "on", "group_left", "group_right", "without", "le",
    }
    candidates = set(_METRIC_NAME_RE.findall(expr))
    return {c for c in candidates if c not in reserved}


def check_metrics_exist(dashboard: dict, known_metrics: set[str] | None = None) -> ValidationResult:
    known_metrics = known_metrics or _load_catalog_metric_names()
    errors = []
    for panel_title, expr in _iter_exprs(dashboard):
        for name in _extract_candidate_metric_names(expr):
            if name not in known_metrics:
                errors.append(
                    f"Panel '{panel_title}': metric '{name}' is not in the approved catalog "
                    f"(agent/metric_catalog.yaml) — possible hallucination"
                )
    return ValidationResult(not errors, errors)


_PARTNER_TYPE_RE = re.compile(r"\b(ssp|dsp)\b", re.IGNORECASE)
_STANDARD_RE = re.compile(r"\bstandard\b", re.IGNORECASE)
_METRIC_KEYWORDS = [
    "latency", "2xx", "success rate", "5xx", "error rate", "errors",
    "qps", "queries per second", "query rate", "request rate", "requests per second",
    "timeout", "system load",
    "bid rate", "win rate", "no-bid", "no bid", "nobid",
    "spend", "budget", "pacing", "ecpm", "cpm", "timeout-to-bid", "timeout to bid",
    "fill rate", "revenue", "rpm", "demand partner", "demand-partner",
    "viewability", "render rate", "ad quality", "blocked creative", "creative",
    "everything", "all metrics",
]


def validate_request_text(text: str) -> ValidationResult:
    """Pre-flight guardrail on the raw onboarding request, run before it ever reaches
    the LLM: require the partner type (SSP/DSP) to be stated explicitly, and require
    either a named metric/panel or the word "standard". Catches vague requests early
    (and saves a Gemini call against a tight daily quota) — this is also what would
    have caught the "DSP-only metric requested for an SSP partner" case, where the
    request named metrics but the renderer silently fell back to the default panel
    set with no explanation once they were filtered out as invalid for that type.
    """
    errors = []
    if not _PARTNER_TYPE_RE.search(text):
        errors.append("Please mention whether this is an SSP or DSP (include the word 'SSP' or 'DSP').")

    text_lower = text.lower()
    has_metric_keyword = any(kw in text_lower for kw in _METRIC_KEYWORDS)
    if not _STANDARD_RE.search(text) and not has_metric_keyword:
        errors.append(
            "Please mention at least one panel/metric to track (e.g. 'latency', "
            "'fill rate'), or say 'standard monitoring'."
        )
    return ValidationResult(not errors, errors)


def validate_dashboard(dashboard: dict) -> ValidationResult:
    dashboard_json = json.dumps(dashboard)
    checks = [
        check_json_valid(dashboard_json),
        check_required_fields(dashboard),
        check_queries_well_formed(dashboard),
        check_metrics_exist(dashboard),
    ]
    all_errors = [e for r in checks for e in r.errors]
    return ValidationResult(not all_errors, all_errors)
