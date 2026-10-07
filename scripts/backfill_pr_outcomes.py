#!/usr/bin/env python3
"""Backfills PR outcomes for agent PRs that were opened/closed before the GitHub
webhook existed (or while the agent was down and missed deliveries).

  docker exec onboarding-agent python -m scripts.backfill_pr_outcomes            # dry run
  docker exec onboarding-agent python -m scripts.backfill_pr_outcomes --apply    # write to store
  ... --apply --candidates   # also open eval-candidate PRs for edited/closed ones

PRs created before lineage stamping have no hidden marker, so a "legacy" lineage is
reconstructed: request text from the PR body, the agent's commit = the PR's first
commit, and the extracted spec inferred from the dashboard in that first commit
(the renderer is deterministic, so this is exact). Prompt/model are "unknown".
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import pr_outcomes  # noqa: E402
from agent.github_pr import _repo, get_file_text, get_json  # noqa: E402
from agent.lineage import parse_marker  # noqa: E402
from agent.spec_inference import infer_spec_from_dashboard  # noqa: E402

_REQUEST_RE = re.compile(r"\*\*Request:\*\*\s*(.+)")
_BRANCH_RE = re.compile(r"^onboard-(.+)-\d{14}$")


def legacy_lineage(pr: dict) -> dict | None:
    m = _BRANCH_RE.match(pr["head"]["ref"])
    if not m:
        return None
    slug = m.group(1)
    commits = get_json(f"/repos/{_repo()}/pulls/{pr['number']}/commits", per_page=100)
    if not commits:
        return None
    first_sha = commits[0]["sha"]
    path = f"dashboards/{slug}.json"
    text = get_file_text(path, first_sha)
    if text is None:
        return None
    req = _REQUEST_RE.search(pr.get("body") or "")
    return {
        "schema_version": 0,
        "request_text": req.group(1).strip() if req else "",
        "prompt_version": "unknown",
        "model": "unknown",
        "fallback_used": False,
        "attempted_models": [],
        "trace_id": None,
        "trace_url": None,
        "slug": slug,
        "dashboard_path": path,
        "branch": pr["head"]["ref"],
        "agent_commit_sha": first_sha,
        "extracted_spec": infer_spec_from_dashboard(json.loads(text)).spec,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write results to the outcome store")
    ap.add_argument("--candidates", action="store_true", help="also open eval-candidate PRs")
    ap.add_argument("--max-pages", type=int, default=10)
    args = ap.parse_args()

    for page in range(1, args.max_pages + 1):
        prs = get_json(f"/repos/{_repo()}/pulls", state="all", per_page=50, page=page)
        if not prs:
            break
        for pr in prs:
            lineage = parse_marker(pr.get("body")) or legacy_lineage(pr)
            if lineage is None:
                continue
            state = "closed" if pr["state"] == "closed" else "opened"
            if not args.apply:
                outcome = pr_outcomes.classify(pr, lineage)["outcome"] if state == "closed" else "open"
                print(f"#{pr['number']:<5} {lineage['slug']:<20} {outcome}")
                continue
            if state == "closed":
                # make sure the open record exists first, then close it
                pr_outcomes.handle_pull_request(pr, "opened", lineage=lineage)
            status = pr_outcomes.handle_pull_request(
                pr, state, lineage=lineage, make_candidates=args.candidates
            )
            print(f"#{pr['number']:<5} {lineage['slug']:<20} {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
