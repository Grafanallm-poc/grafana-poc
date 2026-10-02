"""Turns a plain-English onboarding request into a validated PartnerSpec via OpenAI
structured outputs. The system prompt is versioned on disk (see agent/prompts/) so
prompt changes flow through the same PR + eval-gate as code changes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from openai import OpenAI

from agent.observability import traced_llm_call
from agent.schema import PartnerSpec

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
ACTIVE_PROMPT_VERSION = os.getenv("AGENT_PROMPT_VERSION", "v1")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

_METRIC_ENUM = ["latency", "success_2xx", "error_5xx", "qps", "timeouts", "system_load"]

_RESPONSE_SCHEMA = {
    "name": "partner_spec",
    "strict": True,
    "schema": {
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
                        "aggregation": {"type": ["string", "null"]},
                        "operator": {"type": "string", "enum": [">", ">=", "<", "<="]},
                        "threshold": {"type": "number"},
                        "unit": {"type": ["string", "null"]},
                    },
                    "required": ["metric", "aggregation", "operator", "threshold", "unit"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["partner_name", "partner_type", "metrics", "alerts"],
        "additionalProperties": False,
    },
}


def load_system_prompt(version: str = ACTIVE_PROMPT_VERSION) -> str:
    path = PROMPT_DIR / f"system_prompt.{version}.md"
    return path.read_text()


def _client() -> OpenAI:
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"])


def extract_spec(request_text: str, *, prompt_version: str = ACTIVE_PROMPT_VERSION) -> PartnerSpec:
    system_prompt = load_system_prompt(prompt_version)

    with traced_llm_call(
        name="spec-extraction",
        model=MODEL,
        prompt_version=prompt_version,
        input_text=request_text,
    ) as record:
        response = _client().chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": request_text},
            ],
            response_format={"type": "json_schema", "json_schema": _RESPONSE_SCHEMA},
        )
        content = response.choices[0].message.content
        usage = response.usage
        record(
            output_text=content,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
        )

    data = json.loads(content)
    return PartnerSpec.model_validate(data)
