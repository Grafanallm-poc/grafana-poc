from agent.dashboard_renderer import render_dashboard
from agent.lineage import (build_lineage, parse_marker, parse_spec_block, render_pr_body,
                           stamp_dashboard)
from agent.llm.spec_extractor import ExtractionResult
from agent.schema import PartnerSpec
from agent.validator import validate_dashboard

SPEC = PartnerSpec.model_validate(
    {"partner_name": "Moloco", "partner_type": "DSP", "metrics": ["latency"],
     "alerts": [{"metric": "latency", "aggregation": "p95", "operator": ">", "threshold": 120, "unit": "ms"}]}
)


def _lineage(trace_url="https://us.cloud.langfuse.com/project/p1/traces/t123"):
    ex = ExtractionResult(spec=SPEC, model="gemini-2.5-flash", prompt_version="v2", trace_id="t123",
                          trace_url=trace_url, fallback_used=True,
                          attempted_models=["gemini-3.5-flash", "gemini-2.5-flash"],
                          prompt_tokens=100, completion_tokens=20)
    return build_lineage(request_text="Onboard DSP Moloco --> latency", extraction=ex,
                         slug="moloco", dashboard_path="dashboards/moloco.json")


def test_dashboard_stamp_has_trace_link_and_still_validates():
    lin = _lineage()
    dash = stamp_dashboard(render_dashboard(SPEC), lin)
    s = dash["spec"]
    assert "prompt v2" in s["description"] and "t123" in s["description"]
    assert {"agent-generated", "prompt:v2", "model:gemini-2.5-flash"} <= set(s["tags"])
    assert s["links"][0]["url"] == lin["trace_url"]
    assert validate_dashboard(dash).ok
    # idempotent
    stamp_dashboard(dash, lin)
    assert len(s["links"]) == 1 and s["tags"].count("agent-generated") == 1


def test_no_link_without_langfuse():
    dash = stamp_dashboard(render_dashboard(SPEC), _lineage(trace_url=None))
    assert dash["spec"]["links"] == []


def test_pr_body_roundtrip():
    lin = _lineage()
    lin["agent_commit_sha"] = "deadbeef"
    body = render_pr_body(lin)
    assert "gemini-2.5-flash" in body and "fallback" in body
    assert f"]({lin['trace_url']})" in body
    assert parse_marker(body)["agent_commit_sha"] == "deadbeef"
    assert parse_marker(body)["request_text"] == "Onboard DSP Moloco --> latency"
    assert parse_spec_block(body) == lin["extracted_spec"]
    # GitHub stores edited bodies with CRLF
    assert parse_spec_block(body.replace("\n", "\r\n")) == lin["extracted_spec"]
