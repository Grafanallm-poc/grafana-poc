"""The 'standard SSP/DSP template': turns a validated PartnerSpec into a Grafana
dashboard JSON (classic schema, matching the other hand-built dashboards in this repo).

Panel order is fixed per metric so every partner's dashboard looks the same shape for
the metrics it requests. Most metrics render one panel; a few (e.g. the SSP
demand-partner breakdown) render several, so builders return a list of panels and
layout (id/gridPos) is assigned centrally after flattening.

Business-layer metrics are gated by partner_type: DSP-only metrics are silently
dropped from an SSP spec and vice versa (and the reverse — infra metrics apply to
both). This keeps the LLM from mixing in a metric that doesn't make sense for the
partner type, independent of whatever it actually extracted.
"""
from __future__ import annotations

import os
from typing import Callable

from agent.schema import Alert, Metric, PartnerSpec, PartnerType

DATASOURCE_UID = os.getenv("GRAFANA_PROMETHEUS_DS_UID", "prometheus")
DATASOURCE = {"type": "prometheus", "uid": DATASOURCE_UID}

BOTH = {PartnerType.SSP, PartnerType.DSP}
DSP_ONLY = {PartnerType.DSP}
SSP_ONLY = {PartnerType.SSP}

PANEL_ORDER: list[Metric] = [
    # infra/integration layer
    Metric.QPS,
    Metric.LATENCY,
    Metric.SUCCESS_2XX,
    Metric.ERROR_5XX,
    Metric.TIMEOUTS,
    Metric.SYSTEM_LOAD,
    # DSP business layer
    Metric.BID_RATE,
    Metric.WIN_RATE,
    Metric.NO_BID_RATE,
    Metric.TIMEOUT_TO_BID_RATIO,
    Metric.ECPM,
    Metric.SPEND_PACING,
    # SSP business layer
    Metric.FILL_RATE,
    Metric.REVENUE_RPM,
    Metric.VIEWABILITY_RATE,
    Metric.RENDER_RATE,
    Metric.DEMAND_PARTNER_BREAKDOWN,
    Metric.AD_QUALITY_ISSUES,
]

METRIC_PARTNER_TYPES: dict[Metric, set[PartnerType]] = {
    Metric.QPS: BOTH,
    Metric.LATENCY: BOTH,
    Metric.SUCCESS_2XX: BOTH,
    Metric.ERROR_5XX: BOTH,
    Metric.TIMEOUTS: BOTH,
    Metric.SYSTEM_LOAD: BOTH,
    Metric.BID_RATE: DSP_ONLY,
    Metric.WIN_RATE: DSP_ONLY,
    Metric.NO_BID_RATE: DSP_ONLY,
    Metric.TIMEOUT_TO_BID_RATIO: DSP_ONLY,
    Metric.ECPM: DSP_ONLY,
    Metric.SPEND_PACING: DSP_ONLY,
    Metric.FILL_RATE: SSP_ONLY,
    Metric.REVENUE_RPM: SSP_ONLY,
    Metric.VIEWABILITY_RATE: SSP_ONLY,
    Metric.RENDER_RATE: SSP_ONLY,
    Metric.DEMAND_PARTNER_BREAKDOWN: SSP_ONLY,
    Metric.AD_QUALITY_ISSUES: SSP_ONLY,
}


def _target(expr: str, legend: str, ref_id: str) -> dict:
    return {"expr": expr, "legendFormat": legend, "refId": ref_id}


def _panel(title: str, unit: str, targets: list[dict]) -> dict:
    return {
        "title": title,
        "type": "timeseries",
        "datasource": DATASOURCE,
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "targets": targets,
    }


def _apply_alert_threshold(panel: dict, alert: Alert | None) -> None:
    if alert is None:
        return
    panel["fieldConfig"]["defaults"]["thresholds"] = {
        "mode": "absolute",
        "steps": [
            {"color": "green", "value": None},
            {"color": "red", "value": alert.threshold},
        ],
    }
    panel["title"] += f" (alert {alert.aggregation or ''} {alert.operator.value} {alert.threshold}{alert.unit or ''})".replace("  ", " ")


def _ratio_builder(title: str, numerator: str, denominator: str, *, by_reason: bool = False):
    def build(slug: str, alert: Alert | None) -> list[dict]:
        ratio_expr = (
            f'sum(rate({numerator}{{partner="{slug}"}}[5m])) / '
            f'sum(rate({denominator}{{partner="{slug}"}}[5m]))'
        )
        targets = [_target(ratio_expr, "rate", "A")]
        if by_reason:
            targets.append(
                _target(
                    f'sum by (reason) (rate({numerator}{{partner="{slug}"}}[5m]))',
                    "{{reason}}",
                    "B",
                )
            )
        panel = _panel(title, "percentunit", targets)
        _apply_alert_threshold(panel, alert)
        return [panel]

    return build


def _build_qps(slug: str, alert: Alert | None) -> list[dict]:
    panel = _panel(
        "Queries per Second",
        "reqps",
        [_target(f'sum(rate(partner_requests_total{{partner="{slug}"}}[5m]))', "QPS", "A")],
    )
    _apply_alert_threshold(panel, alert)
    return [panel]


def _build_latency(slug: str, alert: Alert | None) -> list[dict]:
    panel = _panel(
        "Request Latency",
        "s",
        [
            _target(
                f'histogram_quantile(0.50, sum(rate(partner_request_duration_seconds_bucket{{partner="{slug}"}}[5m])) by (le))',
                "p50",
                "A",
            ),
            _target(
                f'histogram_quantile(0.95, sum(rate(partner_request_duration_seconds_bucket{{partner="{slug}"}}[5m])) by (le))',
                "p95",
                "B",
            ),
            _target(
                f'histogram_quantile(0.99, sum(rate(partner_request_duration_seconds_bucket{{partner="{slug}"}}[5m])) by (le))',
                "p99",
                "C",
            ),
        ],
    )
    _apply_alert_threshold(panel, alert)
    return [panel]


def _build_success_2xx(slug: str, alert: Alert | None) -> list[dict]:
    panel = _panel(
        "2XX Success Rate",
        "reqps",
        [_target(f'sum(rate(partner_requests_total{{partner="{slug}", status_class="2xx"}}[5m]))', "2xx", "A")],
    )
    _apply_alert_threshold(panel, alert)
    return [panel]


def _build_error_5xx(slug: str, alert: Alert | None) -> list[dict]:
    panel = _panel(
        "5XX Error Rate",
        "reqps",
        [_target(f'sum(rate(partner_requests_total{{partner="{slug}", status_class="5xx"}}[5m]))', "5xx", "A")],
    )
    _apply_alert_threshold(panel, alert)
    return [panel]


def _build_timeouts(slug: str, alert: Alert | None) -> list[dict]:
    panel = _panel(
        "Timeouts",
        "reqps",
        [_target(f'sum(rate(partner_timeouts_total{{partner="{slug}"}}[5m]))', "timeouts", "A")],
    )
    _apply_alert_threshold(panel, alert)
    return [panel]


def _build_system_load(slug: str, alert: Alert | None) -> list[dict]:
    panel = _panel("System Load", "short", [_target(f'partner_system_load{{partner="{slug}"}}', "load", "A")])
    _apply_alert_threshold(panel, alert)
    return [panel]


def _build_spend_pacing(slug: str, alert: Alert | None) -> list[dict]:
    panel = _panel(
        "Spend vs Daily Budget",
        "currencyUSD",
        [
            _target(f'sum(partner_spend_usd_total{{partner="{slug}"}})', "spend", "A"),
            _target(f'sum(partner_daily_budget_usd{{partner="{slug}"}})', "daily budget", "B"),
        ],
    )
    _apply_alert_threshold(panel, alert)
    return [panel]


def _build_ecpm(slug: str, alert: Alert | None) -> list[dict]:
    panel = _panel("eCPM", "currencyUSD", [_target(f'partner_ecpm_usd{{partner="{slug}"}}', "eCPM", "A")])
    _apply_alert_threshold(panel, alert)
    return [panel]


def _build_ad_quality_issues(slug: str, alert: Alert | None) -> list[dict]:
    panel = _panel(
        "Blocked Creatives by Reason",
        "short",
        [
            _target(
                f'sum by (reason) (rate(partner_blocked_creatives_total{{partner="{slug}"}}[5m]))',
                "{{reason}}",
                "A",
            )
        ],
    )
    _apply_alert_threshold(panel, alert)
    return [panel]


def _build_demand_partner_breakdown(slug: str, alert: Alert | None) -> list[dict]:
    bid_rate_panel = _panel(
        "Bid Rate by Demand Partner",
        "percentunit",
        [
            _target(
                f'sum by (demand_partner) (rate(partner_demand_bids_total{{partner="{slug}"}}[5m])) / '
                f'sum by (demand_partner) (rate(partner_demand_bid_requests_total{{partner="{slug}"}}[5m]))',
                "{{demand_partner}}",
                "A",
            )
        ],
    )
    timeout_rate_panel = _panel(
        "Timeout Rate by Demand Partner",
        "percentunit",
        [
            _target(
                f'sum by (demand_partner) (rate(partner_demand_timeouts_total{{partner="{slug}"}}[5m])) / '
                f'sum by (demand_partner) (rate(partner_demand_bid_requests_total{{partner="{slug}"}}[5m]))',
                "{{demand_partner}}",
                "A",
            )
        ],
    )
    latency_panel = _panel(
        "Latency p95 by Demand Partner",
        "s",
        [
            _target(
                f'histogram_quantile(0.95, sum by (demand_partner, le) '
                f'(rate(partner_demand_latency_seconds_bucket{{partner="{slug}"}}[5m])))',
                "{{demand_partner}}",
                "A",
            )
        ],
    )
    panels = [bid_rate_panel, timeout_rate_panel, latency_panel]
    if alert is not None:
        _apply_alert_threshold(panels[0], alert)
    return panels


_BUILDERS: dict[Metric, Callable[[str, Alert | None], list[dict]]] = {
    Metric.QPS: _build_qps,
    Metric.LATENCY: _build_latency,
    Metric.SUCCESS_2XX: _build_success_2xx,
    Metric.ERROR_5XX: _build_error_5xx,
    Metric.TIMEOUTS: _build_timeouts,
    Metric.SYSTEM_LOAD: _build_system_load,
    Metric.BID_RATE: _ratio_builder("Bid Rate", "partner_bids_total", "partner_bid_requests_total"),
    Metric.WIN_RATE: _ratio_builder("Win Rate", "partner_auctions_won_total", "partner_bids_total"),
    Metric.NO_BID_RATE: _ratio_builder(
        "No-Bid Rate", "partner_no_bids_total", "partner_bid_requests_total", by_reason=True
    ),
    Metric.TIMEOUT_TO_BID_RATIO: _ratio_builder(
        "Timeout-to-Bid Ratio", "partner_timeouts_total", "partner_bids_total"
    ),
    Metric.ECPM: _build_ecpm,
    Metric.SPEND_PACING: _build_spend_pacing,
    Metric.FILL_RATE: _ratio_builder("Fill Rate", "partner_filled_requests_total", "partner_ad_requests_total"),
    Metric.REVENUE_RPM: _ratio_builder("Revenue RPM", "partner_revenue_usd_total", "partner_impressions_total"),
    Metric.VIEWABILITY_RATE: _ratio_builder(
        "Viewability Rate", "partner_viewable_impressions_total", "partner_measured_impressions_total"
    ),
    Metric.RENDER_RATE: _ratio_builder("Render Rate", "partner_rendered_ads_total", "partner_filled_requests_total"),
    Metric.DEMAND_PARTNER_BREAKDOWN: _build_demand_partner_breakdown,
    Metric.AD_QUALITY_ISSUES: _build_ad_quality_issues,
}


def render_dashboard(spec: PartnerSpec) -> dict:
    """Returns the full dashboard.grafana.app/v1 envelope used by this repo's Git Sync
    (apiVersion/kind/metadata.name + spec), matching the hand-built dashboards already
    committed under dashboards/.
    """
    slug = spec.slug()
    alerts_by_metric = {a.metric: a for a in spec.alerts}
    requested = [m for m in PANEL_ORDER if m in spec.metrics] or [
        m for m in PANEL_ORDER if METRIC_PARTNER_TYPES[m] is BOTH
    ]
    applicable = [m for m in requested if spec.partner_type in METRIC_PARTNER_TYPES[m]]

    panels: list[dict] = []
    for metric in applicable:
        panels.extend(_BUILDERS[metric](slug, alerts_by_metric.get(metric)))

    for i, panel in enumerate(panels):
        panel["id"] = i + 1
        panel["gridPos"] = {"h": 8, "w": 12, "x": (i % 2) * 12, "y": (i // 2) * 8}

    return {
        "apiVersion": "dashboard.grafana.app/v1",
        "kind": "Dashboard",
        "metadata": {"name": f"partner-{slug}"},
        "spec": {
            "title": f"{spec.partner_type.value} Onboarding - {spec.partner_name}",
            "description": "",
            "editable": True,
            "graphTooltip": 0,
            "tags": ["partner-onboarding", spec.partner_type.value.lower(), slug],
            "timezone": "browser",
            "schemaVersion": 39,
            "refresh": "30s",
            "time": {"from": "now-6h", "to": "now"},
            "panels": panels,
            "templating": {"list": []},
            "annotations": {"list": []},
        },
    }
