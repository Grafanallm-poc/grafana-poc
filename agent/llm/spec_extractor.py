"""Turns a plain-English onboarding request into a validated PartnerSpec via the
Claude Messages API's structured output (messages.parse against the PartnerSpec
Pydantic model). The system prompt is versioned on disk (see agent/prompts/) so
prompt changes flow through the same PR + eval-gate as code changes.
"""
from __future__ import annotations

import os
from pathlib import Path

from anthropic import Anthropic

from agent.observability import traced_llm_call
from agent.schema import PartnerSpec

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
ACTIVE_PROMPT_VERSION = os.getenv("AGENT_PROMPT_VERSION", "v1")
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")


def load_system_prompt(version: str = ACTIVE_PROMPT_VERSION) -> str:
    path = PROMPT_DIR / f"system_prompt.{version}.md"
    return path.read_text()


def _client() -> Anthropic:
    return Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def extract_spec(request_text: str, *, prompt_version: str = ACTIVE_PROMPT_VERSION) -> PartnerSpec:
    system_prompt = load_system_prompt(prompt_version)

    with traced_llm_call(
        name="spec-extraction",
        model=MODEL,
        prompt_version=prompt_version,
        input_text=request_text,
    ) as record:
        response = _client().messages.parse(
            model=MODEL,
            max_tokens=2048,
            system=system_prompt,
            messages=[{"role": "user", "content": request_text}],
            output_format=PartnerSpec,
        )
        usage = response.usage
        record(
            output_text=response.parsed_output.model_dump_json(),
            prompt_tokens=usage.input_tokens if usage else 0,
            completion_tokens=usage.output_tokens if usage else 0,
        )

    return response.parsed_output
