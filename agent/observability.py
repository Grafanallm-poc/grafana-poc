"""LLMOps plumbing: Prometheus metrics for the agent's own health, and optional
Langfuse tracing for every LLM call's cost/latency.

Langfuse is intentionally best-effort: if LANGFUSE_PUBLIC_KEY/SECRET_KEY aren't
set, tracing is skipped (logged once) so the POC still runs without an account.
Every extraction still gets a trace ID either way (a locally generated one when
Langfuse is off), so the lineage stamp on PRs/dashboards is always populated.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass

from prometheus_client import Counter, Histogram

logger = logging.getLogger("agent.observability")

# ---- Prometheus metrics (scraped by Prometheus, visualized in dashboards/agent-health.json)

ONBOARD_REQUESTS = Counter(
    "agent_onboard_requests_total",
    "Partner onboarding requests handled by the agent",
    # success | request_invalid | spec_invalid | dashboard_invalid | pr_failed
    ["result"],
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

LLM_MODEL_CALLS = Counter(
    "agent_llm_model_calls_total",
    "LLM calls per model and outcome",
    ["model", "outcome"],  # outcome: success | quota_exhausted | unavailable | not_found | error
)

LLM_FALLBACKS = Counter(
    "agent_llm_fallback_total",
    "Times extraction fell through from one model to the next",
    ["from_model", "to_model", "reason"],
)

WEBHOOK_EVENTS = Counter(
    "agent_github_webhook_events_total",
    "GitHub webhook deliveries received",
    ["event", "result"],  # result: processed | ignored | bad_signature | error
)

# Defaults to $0 (Gemini free tier); set these if you move to a paid tier.
_PRICE_PROMPT_PER_1M = float(os.getenv("GEMINI_PRICE_INPUT_PER_1M_USD", "0.0"))
_PRICE_COMPLETION_PER_1M = float(os.getenv("GEMINI_PRICE_OUTPUT_PER_1M_USD", "0.0"))


def estimate_cost_usd(prompt_tokens: int, completion_tokens: int) -> float:
    return (prompt_tokens / 1_000_000) * _PRICE_PROMPT_PER_1M + (
        completion_tokens / 1_000_000
    ) * _PRICE_COMPLETION_PER_1M


_langfuse_client = None
_langfuse_checked = False


def get_langfuse():
    """Returns a Langfuse client (SDK v3+: get_client(), which reads
    LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL from the
    environment) or None if credentials aren't configured. Note LANGFUSE_BASE_URL,
    not the older LANGFUSE_HOST — the v2 SDK's Langfuse(...)/client.generation()
    API is a different major version and does not work against current Langfuse
    Cloud organizations.
    """
    global _langfuse_client, _langfuse_checked
    if _langfuse_checked:
        return _langfuse_client
    _langfuse_checked = True
    if not os.getenv("LANGFUSE_PUBLIC_KEY") or not os.getenv("LANGFUSE_SECRET_KEY"):
        logger.warning("Langfuse keys not set; LLM calls will not be traced to Langfuse.")
        return None
    try:
        from langfuse import get_client

        _langfuse_client = get_client()
    except Exception:  # pragma: no cover - optional dependency
        logger.exception("Failed to initialize Langfuse client; continuing without tracing.")
        _langfuse_client = None
    return _langfuse_client


def langfuse_trace_url(trace_id: str | None) -> str | None:
    """Deep link to a trace in the Langfuse UI. Prefers LANGFUSE_PROJECT_ID (no API
    call needed); otherwise asks the SDK, which looks the project ID up once."""
    if not trace_id:
        return None
    base = os.getenv("LANGFUSE_BASE_URL", "").rstrip("/")
    project_id = os.getenv("LANGFUSE_PROJECT_ID")
    if base and project_id:
        return f"{base}/project/{project_id}/traces/{trace_id}"
    client = get_langfuse()
    if client is None:
        return None
    try:
        return client.get_trace_url(trace_id=trace_id)
    except Exception:  # pragma: no cover
        logger.exception("Could not build Langfuse trace URL")
        return None


@dataclass
class TraceInfo:
    trace_id: str
    trace_url: str | None
    langfuse_enabled: bool


@contextmanager
def extraction_trace(*, name: str, input_text: str, prompt_version: str):
    """Root span for one onboarding extraction (which may contain several model
    attempts when the fallback kicks in). Yields a TraceInfo whose trace_id is the
    Langfuse trace ID when tracing is on, or a local UUID otherwise."""
    client = get_langfuse()
    if client is None:
        yield TraceInfo(trace_id=uuid.uuid4().hex, trace_url=None, langfuse_enabled=False)
        return

    with client.start_as_current_observation(
        as_type="span",
        name=name,
        input=input_text,
        metadata={"prompt_version": prompt_version},
    ) as span:
        trace_id = getattr(span, "trace_id", None) or client.get_current_trace_id() or uuid.uuid4().hex
        info = TraceInfo(trace_id=trace_id, trace_url=None, langfuse_enabled=True)
        try:
            yield info
        finally:
            try:
                client.flush()
            except Exception:  # pragma: no cover
                logger.exception("Langfuse flush failed")
    info.trace_url = langfuse_trace_url(info.trace_id)


@contextmanager
def traced_llm_call(*, name: str, model: str, prompt_version: str, input_text: str):
    """Times one LLM call, records Prometheus metrics, and (best-effort) logs a
    Langfuse generation (nested under the current extraction_trace, if any).

    Usage:
        with traced_llm_call(...) as record:
            response = call_gemini(...)
            record(output_text=response.text, prompt_tokens=.., completion_tokens=..)
    """
    start = time.monotonic()
    result: dict = {}

    def record(*, output_text: str, prompt_tokens: int, completion_tokens: int) -> None:
        result["output_text"] = output_text
        result["prompt_tokens"] = prompt_tokens
        result["completion_tokens"] = completion_tokens

    client = get_langfuse()
    span_cm = (
        client.start_as_current_observation(
            as_type="generation",
            name=name,
            model=model,
            input=input_text,
            metadata={"prompt_version": prompt_version},
        )
        if client is not None
        else nullcontext(None)
    )

    with span_cm as generation:
        error: BaseException | None = None
        try:
            yield record
        except BaseException as exc:
            error = exc
            raise
        finally:
            duration = time.monotonic() - start
            LLM_CALL_DURATION.observe(duration)

            prompt_tokens = result.get("prompt_tokens", 0)
            completion_tokens = result.get("completion_tokens", 0)
            if prompt_tokens or completion_tokens:
                LLM_TOKENS.labels(direction="prompt").inc(prompt_tokens)
                LLM_TOKENS.labels(direction="completion").inc(completion_tokens)
                LLM_COST_USD.inc(estimate_cost_usd(prompt_tokens, completion_tokens))

            if generation is not None:
                try:
                    if error is not None:
                        generation.update(level="ERROR", status_message=str(error)[:500])
                    else:
                        generation.update(
                            output=result.get("output_text"),
                            usage_details={"input": prompt_tokens, "output": completion_tokens},
                        )
                except Exception:  # pragma: no cover - never let tracing break the agent
                    logger.exception("Langfuse trace update failed")
