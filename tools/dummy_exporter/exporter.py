"""Demo-only synthetic traffic generator for onboarded partner dashboards.

This repo's dashboard template (agent/dashboard_renderer.py) queries metrics named
partner_* (infra layer) and a set of DSP/SSP business-layer metrics (see
agent/metric_catalog.yaml for the full list), labeled by partner. No real SSP/DSP
traffic exists in this POC, so this process fakes plausible values for each onboarded
partner slug, purely so demo dashboards show something other than "No data". Not part
of the actual agent.

Business metrics are partner-type-scoped: DSP partners get bid/win/spend-style
metrics, SSP partners get fill/revenue/viewability-style metrics plus a
per-demand-partner breakdown, matching agent/dashboard_renderer.py's gating.
"""
from __future__ import annotations

import os
import random
import time

from prometheus_client import Counter, Gauge, Histogram, start_http_server

SSP_PARTNERS = [p.strip() for p in os.getenv("DUMMY_SSP_PARTNERS", "adcolony,admarvel,talentica1,talentica2").split(",") if p.strip()]
DSP_PARTNERS = [p.strip() for p in os.getenv("DUMMY_DSP_PARTNERS", "moloco,talentica,dt1,dt2,dt3,dt4,dt5").split(",") if p.strip()]
# Which DSPs show up in an SSP's "per demand partner" breakdown.
DEMAND_PARTNERS = [p.strip() for p in os.getenv("DUMMY_DEMAND_PARTNERS", "moloco,dt1,dt2").split(",") if p.strip()]

PORT = int(os.getenv("DUMMY_EXPORTER_PORT", "9091"))
INTERVAL_SECONDS = float(os.getenv("DUMMY_INTERVAL_SECONDS", "5"))

NO_BID_REASONS = ["no_budget", "low_price", "blocked_category", "timeout"]
BLOCKED_CREATIVE_REASONS = ["malware", "policy_violation", "low_quality"]

# --- infra layer (both SSP and DSP) ---
REQUESTS = Counter("partner_requests_total", "Fake request count", ["partner", "status_class"])
DURATION = Histogram("partner_request_duration_seconds", "Fake request latency", ["partner"])
TIMEOUTS = Counter("partner_timeouts_total", "Fake timeout count", ["partner"])
SYSTEM_LOAD = Gauge("partner_system_load", "Fake system load", ["partner"])

# --- DSP business layer ---
BID_REQUESTS = Counter("partner_bid_requests_total", "Fake bid requests received", ["partner"])
BIDS = Counter("partner_bids_total", "Fake bids placed", ["partner"])
AUCTIONS_WON = Counter("partner_auctions_won_total", "Fake auctions won", ["partner"])
NO_BIDS = Counter("partner_no_bids_total", "Fake no-bid count", ["partner", "reason"])
SPEND = Counter("partner_spend_usd_total", "Fake cumulative spend", ["partner"])
DAILY_BUDGET = Gauge("partner_daily_budget_usd", "Fake daily budget", ["partner"])
ECPM = Gauge("partner_ecpm_usd", "Fake eCPM", ["partner"])

# --- SSP business layer ---
AD_REQUESTS = Counter("partner_ad_requests_total", "Fake ad requests received", ["partner"])
FILLED_REQUESTS = Counter("partner_filled_requests_total", "Fake filled ad requests", ["partner"])
REVENUE = Counter("partner_revenue_usd_total", "Fake cumulative revenue", ["partner"])
IMPRESSIONS = Counter("partner_impressions_total", "Fake impressions", ["partner"])
VIEWABLE_IMPRESSIONS = Counter("partner_viewable_impressions_total", "Fake viewable impressions", ["partner"])
MEASURED_IMPRESSIONS = Counter("partner_measured_impressions_total", "Fake measured impressions", ["partner"])
RENDERED_ADS = Counter("partner_rendered_ads_total", "Fake rendered ads", ["partner"])
BLOCKED_CREATIVES = Counter("partner_blocked_creatives_total", "Fake blocked creatives", ["partner", "reason"])

DEMAND_BID_REQUESTS = Counter("partner_demand_bid_requests_total", "Fake bid requests sent per demand partner", ["partner", "demand_partner"])
DEMAND_BIDS = Counter("partner_demand_bids_total", "Fake bids received per demand partner", ["partner", "demand_partner"])
DEMAND_TIMEOUTS = Counter("partner_demand_timeouts_total", "Fake timeouts per demand partner", ["partner", "demand_partner"])
DEMAND_LATENCY = Histogram("partner_demand_latency_seconds", "Fake latency per demand partner", ["partner", "demand_partner"])


def _tick_infra(partner: str) -> None:
    for _ in range(random.randint(5, 25)):
        REQUESTS.labels(partner=partner, status_class="2xx").inc()
    if random.random() < 0.15:
        for _ in range(random.randint(1, 3)):
            REQUESTS.labels(partner=partner, status_class="5xx").inc()
    DURATION.labels(partner=partner).observe(max(0.0, random.gauss(0.08, 0.05)))
    if random.random() < 0.03:
        TIMEOUTS.labels(partner=partner).inc()
    SYSTEM_LOAD.labels(partner=partner).set(random.uniform(0.1, 0.95))


def _tick_dsp(partner: str) -> None:
    bid_requests = random.randint(20, 60)
    BID_REQUESTS.labels(partner=partner).inc(bid_requests)
    bids = int(bid_requests * random.uniform(0.5, 0.9))
    BIDS.labels(partner=partner).inc(bids)
    AUCTIONS_WON.labels(partner=partner).inc(int(bids * random.uniform(0.1, 0.35)))
    no_bids = bid_requests - bids
    for _ in range(max(0, no_bids)):
        NO_BIDS.labels(partner=partner, reason=random.choice(NO_BID_REASONS)).inc()
    spend = random.uniform(1.0, 25.0)
    SPEND.labels(partner=partner).inc(spend)
    DAILY_BUDGET.labels(partner=partner).set(500.0)
    ECPM.labels(partner=partner).set(random.uniform(0.5, 6.0))


def _tick_ssp(partner: str) -> None:
    ad_requests = random.randint(30, 80)
    AD_REQUESTS.labels(partner=partner).inc(ad_requests)
    filled = int(ad_requests * random.uniform(0.6, 0.95))
    FILLED_REQUESTS.labels(partner=partner).inc(filled)
    impressions = int(filled * random.uniform(0.85, 1.0))
    IMPRESSIONS.labels(partner=partner).inc(impressions)
    REVENUE.labels(partner=partner).inc(impressions * random.uniform(0.001, 0.01))
    measured = int(impressions * random.uniform(0.8, 1.0))
    MEASURED_IMPRESSIONS.labels(partner=partner).inc(measured)
    VIEWABLE_IMPRESSIONS.labels(partner=partner).inc(int(measured * random.uniform(0.5, 0.85)))
    RENDERED_ADS.labels(partner=partner).inc(int(filled * random.uniform(0.9, 1.0)))
    if random.random() < 0.08:
        BLOCKED_CREATIVES.labels(partner=partner, reason=random.choice(BLOCKED_CREATIVE_REASONS)).inc()

    for demand_partner in DEMAND_PARTNERS:
        d_bid_requests = random.randint(10, 30)
        DEMAND_BID_REQUESTS.labels(partner=partner, demand_partner=demand_partner).inc(d_bid_requests)
        DEMAND_BIDS.labels(partner=partner, demand_partner=demand_partner).inc(int(d_bid_requests * random.uniform(0.4, 0.85)))
        if random.random() < 0.1:
            DEMAND_TIMEOUTS.labels(partner=partner, demand_partner=demand_partner).inc()
        DEMAND_LATENCY.labels(partner=partner, demand_partner=demand_partner).observe(max(0.0, random.gauss(0.09, 0.04)))


def tick() -> None:
    for partner in DSP_PARTNERS:
        _tick_infra(partner)
        _tick_dsp(partner)
    for partner in SSP_PARTNERS:
        _tick_infra(partner)
        _tick_ssp(partner)


def main() -> None:
    start_http_server(PORT)
    while True:
        tick()
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
