"""Turns a plain-English onboarding request into a validated PartnerSpec via the
Gemini API's structured JSON output (response_json_schema). The schema is hand-written
(flat, no $ref/$defs) rather than taken from PartnerSpec.model_json_schema() directly,
since Gemini's schema support is not guaranteed to match Pydantic's nested-ref output.
The system prompt is versioned on disk (see agent/prompts/) so prompt changes flow
through the same PR + eval-gate as code changes.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from agent.observability import LLM_FALLBACKS, LLM_MODEL_CALLS, extraction_trace, traced_llm_call
from agent.schema import Metric, PartnerSpec

logger = logging.getLogger("agent.llm.spec_extractor")

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
MODELS_CONFIG_PATH = Path(__file__).resolve().parent / "models.yaml"
ACTIVE_PROMPT_VERSION = os.getenv("AGENT_PROMPT_VERSION", "v2")


def load_model_config() -> dict:
    """Pinned primary + ordered fallbacks from agent/llm/models.yaml, with optional
    env overrides (GEMINI_MODEL / GEMINI_FALLBACK_MODELS) for emergencies."""
    cfg = yaml.safe_load(MODELS_CONFIG_PATH.read_text()) or {}
    primary = os.getenv("GEMINI_MODEL") or cfg["primary"]
    env_fallbacks = (os.getenv("GEMINI_FALLBACK_MODELS") or "").strip()
    if env_fallbacks.lower() == "none":
        fallbacks = []
    elif env_fallbacks:
        fallbacks = [m.strip() for m in env_fallbacks.split(",") if m.strip()]
    else:
        fallbacks = list(cfg.get("fallbacks") or [])
    fallbacks = [m for m in fallbacks if m != primary]
    for m in [primary, *fallbacks]:
        if re.search(r"(latest|preview|exp)", m):
            logger.warning(
                "Model %r looks like a moving alias, not a pinned version — lineage and "
                "eval results may not be reproducible.", m
            )
    return {
        "primary": primary,
        "fallbacks": fallbacks,
        "max_attempts_per_model": int(cfg.get("max_attempts_per_model", 3)),
    }


MODEL_CONFIG = load_model_config()
MODEL = MODEL_CONFIG["primary"]  # kept for backwards compatibility

_METRIC_ENUM = [m.value for m in Metric]

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "partner_name": {"type": "string"},
        "partner_type": {"type": "string", "enum": ["SSP", "DSP"]},
        "metrics": {
            "type": "array",
            "items": {"type": "string", "enum": _METRIC_ENUM},
        },
        "alerts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string", "enum": _METRIC_ENUM},
                    "aggregation": {"type": "string"},
                    "operator": {"type": "string", "enum": [">", ">=", "<", "<="]},
                    "threshold": {"type": "number"},
                    "unit": {"type": "string"},
                },
                "required": ["metric", "operator", "threshold"],
            },
        },
    },
    "required": ["partner_name", "partner_type", "metrics", "alerts"],
}


def load_system_prompt(version: str = ACTIVE_PROMPT_VERSION) -> str:
    path = PROMPT_DIR / f"system_prompt.{version}.md"
    return path.read_text()


def _client() -> genai.Client:
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


class AllModelsFailed(RuntimeError):
    pass


def _classify_error(exc: Exception) -> str | None:
    """Returns a fallback reason for errors that should move on to the next model,
    or None for errors that should be raised as-is (bad request, auth, ...)."""
    code = getattr(exc, "code", None)
    if isinstance(exc, genai_errors.ServerError):
        return "unavailable"
    if code == 429:
        return "quota_exhausted"
    if code == 404:
        return "not_found"
    return None


def _generate_with_retry(client: genai.Client, *, max_attempts: int = 3, **kwargs):
    """Gemini's free tier returns intermittent 503s under load; the SDK retries
    internally but can still exhaust those retries, so add a few more attempts here
    with backoff before giving up on this model. 429 (quota) is not retried.
    """
    delay = 2.0
    for attempt in range(1, max_attempts + 1):
        try:
            return client.models.generate_content(**kwargs)
        except genai_errors.ServerError:
            if attempt == max_attempts:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 10.0)


@dataclass
class ExtractionResult:
    spec: PartnerSpec
    model: str  # the model that actually produced the spec
    prompt_version: str
    trace_id: str
    trace_url: str | None
    fallback_used: bool = False
    attempted_models: list[str] = field(default_factory=list)
    raw_output: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


def extract_spec_with_lineage(
    request_text: str,
    *,
    prompt_version: str = ACTIVE_PROMPT_VERSION,
    models: list[str] | None = None,
    client: genai.Client | None = None,
) -> ExtractionResult:
    """Extracts a PartnerSpec, trying the pinned primary model and then each fallback.
    Returns the spec plus lineage (which model/prompt/trace produced it)."""
    system_prompt = load_system_prompt(prompt_version)
    chain = models or [MODEL_CONFIG["primary"], *MODEL_CONFIG["fallbacks"]]
    client = client or _client()
    attempted: list[str] = []
    last_exc: Exception | None = None
    spec: PartnerSpec | None = None
    response = None

    with extraction_trace(
        name="onboarding-extraction", input_text=request_text, prompt_version=prompt_version
    ) as trace:
        for idx, model in enumerate(chain):
            attempted.append(model)
            try:
                with traced_llm_call(
                    name="spec-extraction",
                    model=model,
                    prompt_version=prompt_version,
                    input_text=request_text,
                ) as record:
                    response = _generate_with_retry(
                        client,
                        max_attempts=MODEL_CONFIG["max_attempts_per_model"],
                        model=model,
                        contents=request_text,
                        config=types.GenerateContentConfig(
                            system_instruction=system_prompt,
                            response_mime_type="application/json",
                            response_json_schema=_RESPONSE_SCHEMA,
                        ),
                    )
                    usage = response.usage_metadata
                    prompt_tokens = (usage.prompt_token_count or 0) if usage else 0
                    completion_tokens = (usage.candidates_token_count or 0) if usage else 0
                    record(
                        output_text=response.text,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                    )
            except Exception as exc:  # noqa: BLE001
                reason = _classify_error(exc)
                LLM_MODEL_CALLS.labels(model=model, outcome=reason or "error").inc()
                if reason is None:
                    raise
                last_exc = exc
                nxt = chain[idx + 1] if idx + 1 < len(chain) else None
                if nxt is None:
                    break
                logger.warning("Model %s failed (%s); falling back to %s", model, reason, nxt)
                LLM_FALLBACKS.labels(from_model=model, to_model=nxt, reason=reason).inc()
                continue

            LLM_MODEL_CALLS.labels(model=model, outcome="success").inc()
            spec = PartnerSpec.model_validate(json.loads(response.text))
            break

        if spec is None:
            raise AllModelsFailed(
                f"All configured models failed ({', '.join(attempted)}): {last_exc}"
            ) from last_exc

    return ExtractionResult(
        spec=spec,
        model=attempted[-1],
        prompt_version=prompt_version,
        trace_id=trace.trace_id,
        trace_url=trace.trace_url,
        fallback_used=len(attempted) > 1,
        attempted_models=attempted,
        raw_output=response.text,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def extract_spec(
    request_text: str,
    *,
    prompt_version: str = ACTIVE_PROMPT_VERSION,
    models: list[str] | None = None,
) -> PartnerSpec:
    """Spec only (used by the eval harness)."""
    return extract_spec_with_lineage(request_text, prompt_version=prompt_version, models=models).spec
