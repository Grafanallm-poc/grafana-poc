import json
from pathlib import Path

import pytest

from agent.dashboard_renderer import render_dashboard
from agent.lineage import stamp_dashboard
from agent.schema import PartnerSpec
from agent.spec_inference import effective_metrics, infer_spec_from_dashboard, normalize_spec, spec_diff

CASES = [
    json.loads(line)["expected"]
    for line in (Path(__file__).resolve().parent.parent / "evals" / "testset.jsonl").read_text().splitlines()
    if line.strip()
]


@pytest.mark.parametrize("expected", CASES, ids=[c["partner_name"] for c in CASES])
def test_roundtrip_render_then_infer(expected):
    spec = PartnerSpec.model_validate(expected)
    inferred = infer_spec_from_dashboard(render_dashboard(spec)).spec
    want = normalize_spec(effective_metrics(spec.model_dump(mode="json")))
    got = normalize_spec(inferred)
    assert got["partner_name"] == want["partner_name"]
    assert got["partner_type"] == want["partner_type"]
    assert got["metrics"] == want["metrics"]
    # alerts on metrics that the type gate drops can't be inferred; compare the rest
    want_alerts = [a for a in want["alerts"] if a["metric"] in want["metrics"]]
    assert [(a["metric"], a["operator"], a["threshold"]) for a in got["alerts"]] == [
        (a["metric"], a["operator"], a["threshold"]) for a in want_alerts
    ]


def test_reviewer_edits_are_detected():
    spec = PartnerSpec.model_validate(CASES[0])  # Moloco: latency + 5xx, p95 > 120
    dash = render_dashboard(spec)
    # reviewer bumps the threshold and deletes the 5XX panel
    lat = next(p for p in dash["spec"]["panels"] if p["title"].startswith("Request Latency"))
    lat["fieldConfig"]["defaults"]["thresholds"]["steps"][1]["value"] = 200
    dash["spec"]["panels"] = [lat]
    inferred = infer_spec_from_dashboard(dash)
    assert inferred.spec["metrics"] == ["latency"]
    assert inferred.spec["alerts"][0]["threshold"] == 200
    diffs = spec_diff(spec.model_dump(mode="json"), inferred.spec)
    assert any("error_5xx" in d for d in diffs)
    assert any("alerts" in d for d in diffs)


def test_hand_added_panel_is_reported_and_lineage_stamp_is_ignored():
    spec = PartnerSpec.model_validate(CASES[1])
    dash = stamp_dashboard(
        render_dashboard(spec),
        {"request_text": "x", "prompt_version": "v2", "model": "m", "trace_id": "abc", "generated_at": "t",
         "trace_url": "https://lf/trace/abc"},
    )
    dash["spec"]["panels"].append({"title": "My custom thing", "targets": [{"expr": "up"}]})
    inferred = infer_spec_from_dashboard(dash)
    assert inferred.unmapped_panels == ["My custom thing"]
    assert inferred.spec["partner_name"] == "PubMatic"
