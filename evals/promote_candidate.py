#!/usr/bin/env python3
"""Promotes a reviewer-correction candidate (evals/candidates/*.json) into the main
eval set (evals/testset.jsonl), after a person has confirmed `corrected` is right.

  python -m evals.promote_candidate --list
  python -m evals.promote_candidate evals/candidates/moloco-pr42.json
  python -m evals.promote_candidate evals/candidates/moloco-pr42.json --dry-run

The corrected spec is validated against PartnerSpec and the metric catalog, the
case is appended to testset.jsonl (replacing any existing case with the same
request text), and the candidate file is deleted. Commit the result: the eval
gate (.github/workflows/eval-gate.yml) runs on testset changes, so from that PR on
CI guards against the original mistake.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from agent.schema import PartnerSpec  # noqa: E402

TESTSET = ROOT / "evals" / "testset.jsonl"
CANDIDATES = ROOT / "evals" / "candidates"
CATALOG = ROOT / "agent" / "metric_catalog.yaml"


def _validate(expected: dict) -> dict:
    spec = PartnerSpec.model_validate(expected)
    allowed = set(yaml.safe_load(CATALOG.read_text())["metrics"].keys())
    bad = [m.value for m in spec.metrics if m.value not in allowed]
    if bad:
        raise SystemExit(f"corrected spec uses metrics not in the catalog: {bad}")
    out = spec.model_dump(mode="json")
    # keep the testset's compact style: drop null alert fields
    out["alerts"] = [{k: v for k, v in a.items() if v is not None} for a in out["alerts"]]
    return out


def promote(path: Path, *, dry_run: bool = False) -> dict:
    cand = json.loads(path.read_text())
    if not cand.get("corrected"):
        raise SystemExit(
            f"{path}: `corrected` is empty — fill in the right spec for the request first."
        )
    case = {"request": cand["request"], "expected": _validate(cand["corrected"])}
    source = cand.get("source", {})
    if source.get("pr_url"):
        case["source"] = source["pr_url"]

    lines = [ln for ln in TESTSET.read_text().splitlines() if ln.strip()]
    kept = [ln for ln in lines if json.loads(ln)["request"].strip() != case["request"].strip()]
    replaced = len(kept) != len(lines)
    kept.append(json.dumps(case))

    if dry_run:
        print(json.dumps(case, indent=2))
        print(f"(dry run) would {'replace' if replaced else 'append'} in {TESTSET.relative_to(ROOT)}")
        return case

    TESTSET.write_text("\n".join(kept) + "\n")
    path.unlink()
    print(
        f"{'Replaced' if replaced else 'Added'} case in {TESTSET.relative_to(ROOT)} "
        f"({len(kept)} cases) and removed {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}."
    )
    return case


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("candidate", nargs="?", type=Path)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.list or not args.candidate:
        files = sorted(CANDIDATES.glob("*.json"))
        if not files:
            print("No candidates.")
        for f in files:
            c = json.loads(f.read_text())
            ready = "ready" if c.get("corrected") else "needs corrected spec"
            print(f"{f.relative_to(ROOT)}  [{c.get('source', {}).get('outcome')}, {ready}]  {c['request']}")
            for d in c.get("diff", []):
                print(f"    - {d}")
        return 0

    promote(args.candidate.resolve(), dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
