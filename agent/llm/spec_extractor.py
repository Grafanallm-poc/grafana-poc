"""Turns a plain-English onboarding request into a validated PartnerSpec via the
Gemini API's structured JSON output (response_json_schema). The schema is hand-written
(flat, no $ref/$defs) rather than taken from PartnerSpec.model_json_schema() directly,
since Gemini's schema support is not guaranteed to match Pydantic's nested-ref output.
The system prompt is versioned on disk (see agent/prompts/) so prompt changes flow
through the same PR + eval-gate as code changes.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from agent.observability import traced_llm_call
from agent.schema import Metric, PartnerSpec

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
ACTIVE_PROMPT_VERSION = os.getenv("AGENT_PROMPT_VERSION", "v2")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

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


def _generate_with_retry(client: genai.Client, *, max_attempts: int = 5, **kwargs):
    """Gemini's free tier returns intermittent 503s under load; the SDK retries
    internally but can still exhaust those retries, so add a few more attempts here
    with backoff before giving up.
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


def extract_spec(request_text: str, *, prompt_version: str = ACTIVE_PROMPT_VERSION) -> PartnerSpec:
    system_prompt = load_system_prompt(prompt_version)

    with traced_llm_call(
        name="spec-extraction",
        model=MODEL,
        prompt_version=prompt_version,
        input_text=request_text,
    ) as record:
        client = _client()
        response = _generate_with_retry(
            client,
            model=MODEL,
            contents=request_text,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_json_schema=_RESPONSE_SCHEMA,
            ),
        )
        usage = response.usage_metadata
        record(
            output_text=response.text,
            prompt_tokens=usage.prompt_token_count if usage else 0,
            completion_tokens=usage.candidates_token_count if usage else 0,
        )

    return PartnerSpec.model_validate(json.loads(response.text))
