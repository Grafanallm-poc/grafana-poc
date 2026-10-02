"""LLMOps plumbing: Prometheus metrics for the agent's own health, and optional
Langfuse tracing for every LLM call's cost/latency.

Langfuse is intentionally best-effort: if LANGFUSE_PUBLIC_KEY/SECRET_KEY aren't
set, tracing is skipped (logged once) so the POC still runs without an account.
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager

from prometheus_client import Counter, Histogram

logger = logging.getLogger("agent.observability")

# ---- Prometheus metrics (scraped by Prometheus, visualized in dashboards/agent-health.json)

ONBOARD_REQUESTS = Counter(
    "agent_onboard_requests_total",
    "Partner onboarding requests handled by the agent",
    ["result"],  # success | spec_invalid | dashboard_invalid | pr_failed
)

ONBOARD_DURATION = Histogram(
    "agent_onboard_duration_seconds",
    "End-to-end time to handle an onboarding request",
)

LLM_CALL_DURATION = Histogram(
    "agent_llm_call_duration_seconds",
    "Latency of the spec-extraction LLM call",
)

LLM_COST_USD = Counter(
    "agent_llm_cost_usd_total",
    "Estimated cumulative LLM spend",
)

LLM_TOKENS = Counter(
    "agent_llm_tokens_total",
    "Tokens consumed by LLM calls",
    ["direction"],  # prompt | completion
)

# Defaults to $0 (Gemini free tier); set these if you move GEMINI_MODEL to a paid tier.
_PRICE_PROMPT_PER_1M = float(os.getenv("GEMINI_PRICE_INPUT_PER_1M_USD", "0.0"))
_PRICE_COMPLETION_PER_1M = float(os.getenv("GEMINI_PRICE_OUTPUT_PER_1M_USD", "0.0"))


def estimate_cost_usd(prompt_tokens: int, completion_tokens: int) -> float:
    return (prompt_tokens / 1_000_000) * _PRICE_PROMPT_PER_1M + (
        completion_tokens / 1_000_000
    ) * _PRICE_COMPLETION_PER_1M


_langfuse_client = None
_langfuse_checked = False


def _get_langfuse():
    global _langfuse_client, _langfuse_checked
    if _langfuse_checked:
        return _langfuse_client
    _langfuse_checked = True
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY")
    if not public_key or not secret_key:
        logger.warning("Langfuse keys not set; LLM calls will not be traced to Langfuse.")
        return None
    try:
        from langfuse import Langfuse

        _langfuse_client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        )
    except Exception:  # pragma: no cover - optional dependency
        logger.exception("Failed to initialize Langfuse client; continuing without tracing.")
        _langfuse_client = None
    return _langfuse_client


@contextmanager
def traced_llm_call(*, name: str, model: str, prompt_version: str, input_text: str):
    """Times an LLM call, records Prometheus metrics, and (best-effort) logs a Langfuse trace.

    Usage:
        with traced_llm_call(...) as record:
            response = call_openai(...)
            record(output_text=response.text, prompt_tokens=.., completion_tokens=..)
    """
    start = time.monotonic()
    result: dict = {}

    def record(*, output_text: str, prompt_tokens: int, completion_tokens: int) -> None:
        result["output_text"] = output_text
        result["prompt_tokens"] = prompt_tokens
        result["completion_tokens"] = completion_tokens

    try:
        yield record
    finally:
        duration = time.monotonic() - start
        LLM_CALL_DURATION.observe(duration)

        prompt_tokens = result.get("prompt_tokens", 0)
        completion_tokens = result.get("completion_tokens", 0)
        if prompt_tokens or completion_tokens:
            LLM_TOKENS.labels(direction="prompt").inc(prompt_tokens)
            LLM_TOKENS.labels(direction="completion").inc(completion_tokens)
            LLM_COST_USD.inc(estimate_cost_usd(prompt_tokens, completion_tokens))

        client = _get_langfuse()
        if client is not None:
            try:
                generation = client.generation(
                    name=name,
                    model=model,
                    input=input_text,
                    metadata={"prompt_version": prompt_version},
                )
                generation.end(
                    output=result.get("output_text"),
                    usage={"input": prompt_tokens, "output": completion_tokens, "unit": "TOKENS"},
                )
                client.flush()
            except Exception:  # pragma: no cover - never let tracing break the agent
                logger.exception("Langfuse trace failed")
