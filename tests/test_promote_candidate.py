import json

import pytest

from evals import promote_candidate as pc


def test_promote(tmp_path, monkeypatch):
    ts = tmp_path / "testset.jsonl"
    ts.write_text(json.dumps({"request": "old", "expected": {}}) + "\n")
    monkeypatch.setattr(pc, "TESTSET", ts)
    monkeypatch.setattr(pc, "ROOT", tmp_path)
    cand = tmp_path / "c.json"
    cand.write_text(json.dumps({
        "request": "Onboard DSP Moloco, latency alert p95 > 150ms",
        "corrected": {"partner_name": "Moloco", "partner_type": "DSP", "metrics": ["latency"],
                      "alerts": [{"metric": "latency", "aggregation": "p95", "operator": ">", "threshold": 150, "unit": "ms"}]},
        "source": {"pr_url": "https://github.com/x/y/pull/42"},
    }))
    pc.promote(cand)
    lines = [json.loads(l) for l in ts.read_text().splitlines()]
    assert len(lines) == 2 and lines[1]["expected"]["alerts"][0]["threshold"] == 150
    assert lines[1]["source"].endswith("/42")
    assert not cand.exists()


def test_refuses_empty_corrected(tmp_path):
    cand = tmp_path / "c.json"
    cand.write_text(json.dumps({"request": "r", "corrected": None}))
    with pytest.raises(SystemExit):
        pc.promote(cand)
