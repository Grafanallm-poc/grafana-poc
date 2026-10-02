"""The 'standard SSP/DSP template': turns a validated PartnerSpec into a Grafana
dashboard JSON (classic schema, matching the other hand-built dashboards in this repo).

One function builds one panel per requested metric; order is fixed so every partner's
dashboard looks the same shape, per the "every partner gets the same standard
dashboard" requirement.
"""
from __future__ import annotations

import os
from typing import Callable

from agent.schema import Alert, Metric, PartnerSpec

DATASOURCE_UID = os.getenv("GRAFANA_PROMETHEUS_DS_UID", "prometheus")
DATASOURCE = {"type": "prometheus", "uid": DATASOURCE_UID}

PANEL_ORDER: list[Metric] = [
    Metric.QPS,
    Metric.LATENCY,
    Metric.SUCCESS_2XX,
    Metric.ERROR_5XX,
    Metric.TIMEOUTS,
    Metric.SYSTEM_LOAD,
]


def _target(expr: str, legend: str, ref_id: str) -> dict:
    return {"expr": expr, "legendFormat": legend, "refId": ref_id}


def _base_panel(panel_id: int, title: str, x: int, y: int, unit: str) -> dict:
    return {
        "id": panel_id,
        "title": title,
        "type": "timeseries",
        "datasource": DATASOURCE,
        "gridPos": {"h": 8, "w": 12, "x": x, "y": y},
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "targets": [],
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


def _build_qps(slug: str, panel_id: int, x: int, y: int, alert: Alert | None) -> dict:
    panel = _base_panel(panel_id, "Queries per Second", x, y, "reqps")
    panel["targets"] = [
        _target(f'sum(rate(partner_requests_total{{partner="{slug}"}}[5m]))', "QPS", "A")
    ]
    _apply_alert_threshold(panel, alert)
    return panel


def _build_latency(slug: str, panel_id: int, x: int, y: int, alert: Alert | None) -> dict:
    panel = _base_panel(panel_id, "Request Latency", x, y, "s")
    panel["targets"] = [
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
    ]
    _apply_alert_threshold(panel, alert)
    return panel


def _build_success_2xx(slug: str, panel_id: int, x: int, y: int, alert: Alert | None) -> dict:
    panel = _base_panel(panel_id, "2XX Success Rate", x, y, "reqps")
    panel["targets"] = [
        _target(
            f'sum(rate(partner_requests_total{{partner="{slug}", status_class="2xx"}}[5m]))',
            "2xx",
            "A",
        )
    ]
    _apply_alert_threshold(panel, alert)
    return panel


def _build_error_5xx(slug: str, panel_id: int, x: int, y: int, alert: Alert | None) -> dict:
    panel = _base_panel(panel_id, "5XX Error Rate", x, y, "reqps")
    panel["targets"] = [
        _target(
            f'sum(rate(partner_requests_total{{partner="{slug}", status_class="5xx"}}[5m]))',
            "5xx",
            "A",
        )
    ]
    _apply_alert_threshold(panel, alert)
    return panel


def _build_timeouts(slug: str, panel_id: int, x: int, y: int, alert: Alert | None) -> dict:
    panel = _base_panel(panel_id, "Timeouts", x, y, "reqps")
    panel["targets"] = [
        _target(f'sum(rate(partner_timeouts_total{{partner="{slug}"}}[5m]))', "timeouts", "A")
    ]
    _apply_alert_threshold(panel, alert)
    return panel


def _build_system_load(slug: str, panel_id: int, x: int, y: int, alert: Alert | None) -> dict:
    panel = _base_panel(panel_id, "System Load", x, y, "short")
    panel["targets"] = [
        _target(f'partner_system_load{{partner="{slug}"}}', "load", "A")
    ]
    _apply_alert_threshold(panel, alert)
    return panel


_BUILDERS: dict[Metric, Callable] = {
    Metric.QPS: _build_qps,
    Metric.LATENCY: _build_latency,
    Metric.SUCCESS_2XX: _build_success_2xx,
    Metric.ERROR_5XX: _build_error_5xx,
    Metric.TIMEOUTS: _build_timeouts,
    Metric.SYSTEM_LOAD: _build_system_load,
}


def render_dashboard(spec: PartnerSpec) -> dict:
    """Returns the full dashboard.grafana.app/v1 envelope used by this repo's Git Sync
    (apiVersion/kind/metadata.name + spec), matching the hand-built dashboards already
    committed under dashboards/.
    """
    slug = spec.slug()
    alerts_by_metric = {a.metric: a for a in spec.alerts}
    metrics = [m for m in PANEL_ORDER if m in spec.metrics] or PANEL_ORDER

    panels = []
    for i, metric in enumerate(metrics):
        x = (i % 2) * 12
        y = (i // 2) * 8
        panel = _BUILDERS[metric](slug, i + 1, x, y, alerts_by_metric.get(metric))
        panels.append(panel)

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
