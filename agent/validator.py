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
_METRIC_NAME_RE = re.compile(r"\b([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(?:\{|\[|\)|\s|$)")


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)


def _load_catalog_metric_names() -> set[str]:
    catalog = yaml.safe_load(_CATALOG_PATH.read_text())
    return {entry["promql_metric"] for entry in catalog["metrics"].values()}


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
