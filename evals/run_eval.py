#!/usr/bin/env python3
"""LLMOps eval gate: runs every prompt in evals/testset.jsonl through the live
spec-extraction pipeline, scores accuracy against the expected spec, and checks for
hallucinated metrics (anything outside agent/metric_catalog.yaml). Exits non-zero if
accuracy drops below EVAL_MIN_SCORE or any hallucination is found, which is what
.github/workflows/eval-gate.yml uses to block a bad prompt/model change from merging.

Run locally:  GEMINI_API_KEY=... python -m evals.run_eval
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from agent.llm.spec_extractor import MODEL_CONFIG, extract_spec  # noqa: E402

TESTSET_PATH = Path(__file__).resolve().parent / "testset.jsonl"
RESULTS_PATH = Path(__file__).resolve().parent / "results.json"
CATALOG_PATH = Path(__file__).resolve().parent.parent / "agent" / "metric_catalog.yaml"
MIN_SCORE = float(os.getenv("EVAL_MIN_SCORE", "0.8"))
# Score one model at a time with fallback disabled, so a quota hit mid-run can't
# silently swap in a different model. Defaults to the pinned primary; set
# EVAL_MODEL=<fallback id> to gate the fallback model too.
EVAL_MODEL = os.getenv("EVAL_MODEL") or MODEL_CONFIG["primary"]

# Same catalog the validator enforces at PR time — a metric extracted here that
# isn't a key in this file is, by definition, hallucinated.
_ALLOWED_METRICS = set(yaml.safe_load(CATALOG_PATH.read_text())["metrics"].keys())


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b) if (a | b) else 1.0


def _alerts_match(expected: list[dict], actual: list[dict]) -> float:
    if not expected and not actual:
        return 1.0
    if not expected or not actual:
        return 0.0
    hits = 0
    for exp in expected:
        for act in actual:
            if (
                exp["metric"] == act["metric"]
                and exp["operator"] == act["operator"]
                and float(exp["threshold"]) == float(act["threshold"])
            ):
                hits += 1
                break
    return hits / len(expected)


def score_case(expected: dict, actual: dict) -> float:
    name_score = 1.0 if expected["partner_name"].strip().lower() == actual["partner_name"].strip().lower() else 0.0
    type_score = 1.0 if expected["partner_type"] == actual["partner_type"] else 0.0
    metrics_score = _jaccard(set(expected["metrics"]), set(actual["metrics"]))
    alerts_score = _alerts_match(expected["alerts"], actual["alerts"])
    return 0.3 * name_score + 0.2 * type_score + 0.3 * metrics_score + 0.2 * alerts_score


def main() -> int:
    cases = [json.loads(line) for line in TESTSET_PATH.read_text().splitlines() if line.strip()]
    results = []
    hallucinations: list[str] = []

    for case in cases:
        spec = extract_spec(case["request"], models=[EVAL_MODEL])
        actual = spec.model_dump(mode="json")

        for m in actual["metrics"]:
            if m not in _ALLOWED_METRICS:
                hallucinations.append(f"{case['request']!r} -> hallucinated metric {m!r}")

        score = score_case(case["expected"], actual)
        results.append({"request": case["request"], "score": score, "actual": actual})

    avg_score = sum(r["score"] for r in results) / len(results)
    RESULTS_PATH.write_text(
        json.dumps({"model": EVAL_MODEL, "average_score": avg_score, "cases": results}, indent=2)
    )

    print(f"Model: {EVAL_MODEL}")
    print(f"Eval cases: {len(results)}")
    for r in results:
        print(f"  [{r['score']:.2f}] {r['request']}")
    print(f"Average score: {avg_score:.3f} (minimum required: {MIN_SCORE})")
    print(f"Hallucinated metrics: {len(hallucinations)}")
    for h in hallucinations:
        print(f"  - {h}")

    if hallucinations:
        print("FAIL: hallucinated metrics detected")
        return 1
    if avg_score < MIN_SCORE:
        print("FAIL: average score below threshold")
        return 1

    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
