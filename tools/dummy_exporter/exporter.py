"""Demo-only synthetic traffic generator for onboarded partner dashboards.

This repo's dashboard template (agent/dashboard_renderer.py) queries metrics named
partner_requests_total / partner_request_duration_seconds_bucket / partner_timeouts_total
/ partner_system_load, labeled by partner. No real SSP/DSP traffic exists in this POC,
so this process fakes plausible values for each onboarded partner slug, purely so
demo dashboards show something other than "No data". Not part of the actual agent.
"""
from __future__ import annotations

import os
import random
import time

from prometheus_client import Counter, Gauge, Histogram, start_http_server

_DEFAULT_PARTNERS = (
    "moloco,talentica,"
    # SSPs
    "adcolony,admarvel,talentica1,talentica2,"
    # DSPs
    "dt1,dt2,dt3,dt4,dt5"
)
PARTNERS = [p.strip() for p in os.getenv("DUMMY_PARTNERS", _DEFAULT_PARTNERS).split(",") if p.strip()]
PORT = int(os.getenv("DUMMY_EXPORTER_PORT", "9091"))
INTERVAL_SECONDS = float(os.getenv("DUMMY_INTERVAL_SECONDS", "5"))

REQUESTS = Counter("partner_requests_total", "Fake request count", ["partner", "status_class"])
DURATION = Histogram("partner_request_duration_seconds", "Fake request latency", ["partner"])
TIMEOUTS = Counter("partner_timeouts_total", "Fake timeout count", ["partner"])
SYSTEM_LOAD = Gauge("partner_system_load", "Fake system load", ["partner"])


def tick() -> None:
    for partner in PARTNERS:
        for _ in range(random.randint(5, 25)):
            REQUESTS.labels(partner=partner, status_class="2xx").inc()
        if random.random() < 0.15:
            for _ in range(random.randint(1, 3)):
                REQUESTS.labels(partner=partner, status_class="5xx").inc()
        DURATION.labels(partner=partner).observe(max(0.0, random.gauss(0.08, 0.05)))
        if random.random() < 0.03:
            TIMEOUTS.labels(partner=partner).inc()
        SYSTEM_LOAD.labels(partner=partner).set(random.uniform(0.1, 0.95))


def main() -> None:
    start_http_server(PORT)
    while True:
        tick()
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
