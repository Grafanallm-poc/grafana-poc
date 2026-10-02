"""Structured spec the LLM must produce from a plain-English onboarding request."""
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class PartnerType(str, Enum):
    SSP = "SSP"
    DSP = "DSP"


class Metric(str, Enum):
    LATENCY = "latency"
    SUCCESS_2XX = "success_2xx"
    ERROR_5XX = "error_5xx"
    QPS = "qps"
    TIMEOUTS = "timeouts"
    SYSTEM_LOAD = "system_load"


class AlertOperator(str, Enum):
    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="


class Alert(BaseModel):
    metric: Metric
    # e.g. "p95", "p99", "avg" — only meaningful for latency today
    aggregation: Optional[str] = Field(default=None, description="p50/p95/p99/avg, latency only")
    operator: AlertOperator
    threshold: float
    unit: Optional[str] = Field(default=None, description="ms, percent, count, etc.")


class PartnerSpec(BaseModel):
    partner_name: str
    partner_type: PartnerType
    metrics: list[Metric] = Field(default_factory=list)
    alerts: list[Alert] = Field(default_factory=list)

    def slug(self) -> str:
        return "".join(c.lower() if c.isalnum() else "-" for c in self.partner_name).strip("-")
