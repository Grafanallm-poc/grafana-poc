"""Reverse of dashboard_renderer: infer the PartnerSpec a dashboard represents.

Used by the feedback loop: when a reviewer edits an agent-generated dashboard
before merging, the merged JSON is turned back into a spec and compared with what
the LLM extracted. Because the renderer is deterministic, the mapping from panel
titles/queries back to metrics is unambiguous for anything the template produces.

Panels the template could not have produced (a reviewer added a hand-written
panel) are returned in `unmapped_panels` for a human to look at.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from agent.dashboard_renderer import _BUILDERS, PANEL_ORDER
from agent.schema import Metric, PartnerType

_PROBE_SLUG = "__probe__"
_ALERT_SUFFIX_RE = re.compile(
    r"\s*\(alert\s*(?P<agg>p\d{2}|avg)?\s*(?P<op>>=|<=|>|<)\s*(?P<thr>-?[\d.]+)\s*(?P<unit>[^)\s]*)\s*\)\s*$"
)
_METRIC_NAME_RE = re.compile(r"\b([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(?:\{|\[)")


def _signature(panel: dict, slug: str) -> tuple:
    """Query-shape fingerprint of a panel, independent of partner slug and title."""
    exprs = []
    for t in panel.get("targets", []):
        expr = t.get("expr", "")
        expr = re.sub(r'partner="[^"]*"', 'partner="*"', expr)
        exprs.append(re.sub(r"\s+", "", expr))
    return tuple(sorted(exprs))


def _build_index() -> tuple[dict[str, Metric], dict[tuple, Metric]]:
    by_title: dict[str, Metric] = {}
    by_sig: dict[tuple, Metric] = {}
    for metric in PANEL_ORDER:
        for panel in _BUILDERS[metric](_PROBE_SLUG, None):
            by_title[panel["title"].lower()] = metric
            by_sig[_signature(panel, _PROBE_SLUG)] = metric
    return by_title, by_sig


_BY_TITLE, _BY_SIG = _build_index()


@dataclass
class InferredSpec:
    spec: dict
    unmapped_panels: list[str] = field(default_factory=list)


def _parse_alert(panel: dict, metric: Metric) -> dict | None:
    title = panel.get("title", "")
    m = _ALERT_SUFFIX_RE.search(title)
    steps = (
        panel.get("fieldConfig", {}).get("defaults", {}).get("thresholds", {}).get("steps", [])
    )
    step_values = [s.get("value") for s in steps if s.get("value") is not None]
    if not m and not step_values:
        return None
    # The threshold step is what Grafana actually renders, so it wins over the title
    # text if a reviewer changed one but not the other.
    threshold = float(step_values[-1]) if step_values else float(m.group("thr"))
    return {
        "metric": metric.value,
        "aggregation": (m.group("agg") if m else None) or None,
        "operator": m.group("op") if m else ">",
        "threshold": threshold,
        "unit": (m.group("unit") if m else None) or None,
    }


def infer_spec_from_dashboard(dashboard: dict) -> InferredSpec:
    spec = dashboard.get("spec", dashboard)
    title = spec.get("title", "")
    tags = [t.lower() for t in spec.get("tags", [])]

    partner_type = None
    tmatch = re.match(r"\s*(SSP|DSP)\s+Onboarding\s*-\s*(.+?)\s*$", title, re.IGNORECASE)
    if tmatch:
        partner_type = tmatch.group(1).upper()
        partner_name = tmatch.group(2)
    else:
        partner_name = title
    if partner_type is None:
        partner_type = "DSP" if "dsp" in tags else "SSP" if "ssp" in tags else None

    metrics: list[str] = []
    alerts: list[dict] = []
    unmapped: list[str] = []
    for panel in spec.get("panels", []):
        if panel.get("type") == "row":
            continue
        ptitle = panel.get("title", "")
        base_title = _ALERT_SUFFIX_RE.sub("", ptitle).strip().lower()
        metric = _BY_TITLE.get(base_title) or _BY_SIG.get(_signature(panel, ""))
        if metric is None:
            unmapped.append(ptitle or "<untitled panel>")
            continue
        if metric.value not in metrics:
            metrics.append(metric.value)
        alert = _parse_alert(panel, metric)
        if alert and not any(a["metric"] == alert["metric"] for a in alerts):
            alerts.append(alert)

    order = {m.value: i for i, m in enumerate(PANEL_ORDER)}
    metrics.sort(key=lambda m: order.get(m, 999))
    return InferredSpec(
        spec={
            "partner_name": partner_name,
            "partner_type": partner_type or PartnerType.SSP.value,
            "metrics": metrics,
            "alerts": alerts,
        },
        unmapped_panels=unmapped,
    )


def normalize_spec(spec: dict | None) -> dict | None:
    """Canonical form for comparing two specs (order-insensitive, numeric thresholds)."""
    if spec is None:
        return None
    alerts = sorted(
        (
            {
                "metric": a.get("metric"),
                "aggregation": (a.get("aggregation") or None),
                "operator": a.get("operator"),
                "threshold": float(a.get("threshold")) if a.get("threshold") is not None else None,
                "unit": (a.get("unit") or None),
            }
            for a in spec.get("alerts", [])
        ),
        key=lambda a: (a["metric"] or "", a["operator"] or ""),
    )
    return {
        "partner_name": (spec.get("partner_name") or "").strip(),
        "partner_type": spec.get("partner_type"),
        "metrics": sorted(spec.get("metrics", [])),
        "alerts": alerts,
    }


def effective_metrics(spec: dict) -> dict:
    """What the renderer would actually show: empty metrics => default infra set,
    and business metrics that don't apply to the partner type are dropped. Comparing
    effective specs avoids flagging "no metrics" vs "the 6 infra metrics" as a diff."""
    from agent.dashboard_renderer import BOTH, METRIC_PARTNER_TYPES

    out = dict(spec)
    ptype = PartnerType(spec["partner_type"]) if spec.get("partner_type") else None
    metrics = [Metric(m) for m in spec.get("metrics", []) if m in Metric._value2member_map_]
    if not metrics:
        metrics = [m for m in PANEL_ORDER if METRIC_PARTNER_TYPES[m] is BOTH]
    if ptype is not None:
        metrics = [m for m in metrics if ptype in METRIC_PARTNER_TYPES[m]]
    out["metrics"] = [m.value for m in metrics]
    return out


def spec_diff(before: dict, after: dict) -> list[str]:
    """Human-readable list of differences between two specs."""
    a, b = normalize_spec(before), normalize_spec(after)
    diffs = []
    if a["partner_name"].lower() != b["partner_name"].lower():
        diffs.append(f'partner_name: {a["partner_name"]!r} -> {b["partner_name"]!r}')
    if a["partner_type"] != b["partner_type"]:
        diffs.append(f'partner_type: {a["partner_type"]} -> {b["partner_type"]}')
    added = sorted(set(b["metrics"]) - set(a["metrics"]))
    removed = sorted(set(a["metrics"]) - set(b["metrics"]))
    if added:
        diffs.append(f"metrics added by reviewer: {', '.join(added)}")
    if removed:
        diffs.append(f"metrics removed by reviewer: {', '.join(removed)}")
    if a["alerts"] != b["alerts"]:
        diffs.append(f'alerts: {a["alerts"]} -> {b["alerts"]}')
    return diffs
