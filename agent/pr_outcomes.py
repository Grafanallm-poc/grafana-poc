"""PR-outcome tracking + reviewer-corrections feedback loop.

Fed by the GitHub webhook (POST /webhooks/github in agent/app.py) and by
scripts/backfill_pr_outcomes.py for PRs closed before the webhook existed.

For every agent PR (identified by the hidden lineage marker in its description):

  opened  -> recorded as "open"
  closed  -> classified as one of
               merged_unchanged  merged, dashboard + spec block exactly as generated
               merged_edited     merged, but a reviewer changed the dashboard JSON
                                 or the spec block first
               closed_unmerged   closed without merging (rejected)

When a PR is edited or closed, the original request, the LLM's extracted spec and
the reviewer's corrected spec are written to evals/candidates/ (via a new PR, since
this service runs on EC2, not in a checkout). A person confirms the expected answer
and promotes it into evals/testset.jsonl with `python -m evals.promote_candidate`,
after which the CI eval gate guards against that mistake.

Outcomes, time-to-merge and cost-per-dashboard (from Langfuse) are exposed to
Prometheus by AgentPRCollector, computed from the persistent store.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

import requests
from prometheus_client.core import GaugeMetricFamily, HistogramMetricFamily, SummaryMetricFamily
from prometheus_client.registry import Collector

from agent import store
from agent.github_pr import GitHubPRError, get_file_text, open_pr
from agent.lineage import parse_marker, parse_spec_block
from agent.spec_inference import (
    effective_metrics,
    infer_spec_from_dashboard,
    normalize_spec,
    spec_diff,
)

logger = logging.getLogger("agent.pr_outcomes")

OUTCOMES = ("open", "merged_unchanged", "merged_edited", "closed_unmerged")
CANDIDATES_DIR = "evals/candidates"
_TTM_BUCKETS = [300, 900, 1800, 3600, 4 * 3600, 12 * 3600, 86400, 3 * 86400, 7 * 86400]


# ---------------------------------------------------------------- webhook auth

def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


# ---------------------------------------------------------------- helpers

def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _same_json(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return a == b
    try:
        return json.loads(a) == json.loads(b)
    except json.JSONDecodeError:
        return a.strip() == b.strip()


def _specs_equivalent(a: dict | None, b: dict | None) -> bool:
    if a is None or b is None:
        return a == b
    try:
        na = normalize_spec(effective_metrics(a))
        nb = normalize_spec(effective_metrics(b))
    except (KeyError, ValueError):
        return normalize_spec(a) == normalize_spec(b)
    na["partner_name"] = na["partner_name"].lower()
    nb["partner_name"] = nb["partner_name"].lower()
    return na == nb


# ---------------------------------------------------------------- classification

def classify(pr: dict, lineage: dict) -> dict:
    """Classifies a *closed* PR. `pr` is the GitHub pull_request object.
    Returns outcome + the reviewer-corrected spec (if any) and where it came from."""
    extracted = lineage.get("extracted_spec")
    body_spec = parse_spec_block(pr.get("body"))
    spec_block_edited = body_spec is not None and not _specs_equivalent(body_spec, extracted)

    result = {
        "outcome": None,
        "corrected_spec": None,
        "correction_source": None,
        "notes": [],
    }

    final_dashboard = None
    dashboard_edited = False
    path = lineage.get("dashboard_path")
    if pr.get("merged"):
        original = get_file_text(path, lineage["agent_commit_sha"]) if lineage.get("agent_commit_sha") else None
        final_text = get_file_text(path, pr["head"]["sha"])
        dashboard_edited = not _same_json(original, final_text)
        if final_text is not None:
            try:
                final_dashboard = json.loads(final_text)
            except json.JSONDecodeError:
                result["notes"].append("merged dashboard JSON is not valid JSON")
        else:
            result["notes"].append("dashboard file was removed before merge")

        result["outcome"] = "merged_edited" if (dashboard_edited or spec_block_edited) else "merged_unchanged"
    else:
        result["outcome"] = "closed_unmerged"

    # Reviewer's corrected spec — an explicitly edited spec block wins, since it
    # states intent directly; otherwise infer it from the merged dashboard.
    if spec_block_edited:
        result["corrected_spec"] = body_spec
        result["correction_source"] = "pr_spec_block"
    elif dashboard_edited and final_dashboard is not None:
        inferred = infer_spec_from_dashboard(final_dashboard)
        if inferred.unmapped_panels:
            result["notes"].append(
                "panels not produced by the template (hand-added?): " + ", ".join(inferred.unmapped_panels)
            )
        if _specs_equivalent(inferred.spec, extracted):
            result["notes"].append(
                "dashboard was edited but the spec is unchanged — a template/layout tweak, "
                "not an extraction error"
            )
        else:
            result["corrected_spec"] = inferred.spec
            result["correction_source"] = "merged_dashboard"
    return result


# ---------------------------------------------------------------- candidates

def build_candidate(pr: dict, lineage: dict, classification: dict) -> dict:
    extracted = lineage.get("extracted_spec")
    corrected = classification["corrected_spec"]
    return {
        "status": "needs_review",
        "request": lineage["request_text"],
        "extracted": extracted,
        "corrected": corrected,
        "correction_source": classification["correction_source"],
        "diff": spec_diff(extracted, corrected) if (extracted and corrected) else [],
        "notes": classification["notes"],
        "source": {
            "pr_number": pr["number"],
            "pr_url": pr["html_url"],
            "outcome": classification["outcome"],
            "closed_at": pr.get("closed_at"),
            "closed_by": (pr.get("merged_by") or {}).get("login") if pr.get("merged") else None,
        },
        "lineage": {
            "prompt_version": lineage.get("prompt_version"),
            "model": lineage.get("model"),
            "trace_id": lineage.get("trace_id"),
            "trace_url": lineage.get("trace_url"),
        },
        "instructions": (
            "Confirm `corrected` is the right answer for `request` (fill it in if null — "
            "e.g. the PR was closed without a fix), then run "
            "`python -m evals.promote_candidate <this file>` to add it to evals/testset.jsonl."
        ),
    }


def _should_make_candidate(classification: dict) -> bool:
    if classification["outcome"] == "closed_unmerged":
        return True
    return classification["outcome"] == "merged_edited" and classification["corrected_spec"] is not None


def open_candidate_pr(pr: dict, lineage: dict, candidate: dict) -> tuple[str, str | None]:
    slug = lineage.get("slug") or "partner"
    path = f"{CANDIDATES_DIR}/{slug}-pr{pr['number']}.json"
    if os.getenv("EVAL_CANDIDATE_MODE", "pr").lower() != "pr":
        return path, None
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    diff_md = "\n".join(f"- {d}" for d in candidate["diff"]) or "- (no corrected spec yet — fill it in)"
    body = (
        f"Candidate eval case from #{pr['number']} ({candidate['source']['outcome']}).\n\n"
        f"**Request:** {candidate['request']}\n\n"
        f"**Reviewer correction** (source: `{candidate['correction_source']}`):\n{diff_md}\n\n"
        f"Prompt `{candidate['lineage']['prompt_version']}` · model `{candidate['lineage']['model']}` · "
        + (
            f"[Langfuse trace]({candidate['lineage']['trace_url']})"
            if candidate["lineage"].get("trace_url")
            else f"trace `{candidate['lineage']['trace_id']}`"
        )
        + "\n\nTo accept: check `corrected`, then run `python -m evals.promote_candidate "
        f"{path}` on this branch and push — the eval gate will then run the new case."
    )
    created = open_pr(
        branch=f"eval-candidate-{slug}-pr{pr['number']}-{ts}",
        files={path: json.dumps(candidate, indent=2) + "\n"},
        commit_message=f"Add eval candidate from PR #{pr['number']}",
        title=f"Eval candidate: {lineage['extracted_spec'].get('partner_name', slug)} (from #{pr['number']})",
        body=body,
        labels=["eval-candidate"],
    )
    return path, created["pr_url"]


# ---------------------------------------------------------------- event handling

def record_opened(*, pr_number: int, pr_url: str, lineage: dict, created_at: float | None = None) -> None:
    store.upsert_pr(
        pr_number,
        pr_url=pr_url,
        slug=lineage.get("slug"),
        partner_name=(lineage.get("extracted_spec") or {}).get("partner_name"),
        request_text=lineage.get("request_text"),
        prompt_version=lineage.get("prompt_version"),
        model=lineage.get("model"),
        fallback_used=lineage.get("fallback_used", False),
        trace_id=lineage.get("trace_id"),
        trace_url=lineage.get("trace_url"),
        branch=lineage.get("branch"),
        agent_commit_sha=lineage.get("agent_commit_sha"),
        created_at=created_at or time.time(),
        extracted_spec=lineage.get("extracted_spec"),
        cost_usd=lineage.get("estimated_cost_usd"),
        cost_source="estimate" if lineage.get("estimated_cost_usd") is not None else None,
    )


def handle_pull_request(
    pr: dict, action: str, *, lineage: dict | None = None, make_candidates: bool = True
) -> str:
    """Processes one pull_request event. Returns a short status string."""
    lineage = lineage or parse_marker(pr.get("body"))
    if lineage is None:
        return "ignored: not an agent PR"

    existing = store.get_pr(pr["number"])
    if existing is None or action == "opened":
        record_opened(
            pr_number=pr["number"], pr_url=pr["html_url"], lineage=lineage, created_at=_ts(pr.get("created_at"))
        )
        existing = store.get_pr(pr["number"])

    if action == "reopened":
        store.upsert_pr(pr["number"], outcome="open", closed_at=None)
        return "reopened"
    if action != "closed":
        return f"recorded {action}"
    if existing and existing.get("outcome") not in (None, "open") and existing.get("closed_at"):
        return f"already processed ({existing['outcome']})"

    classification = classify(pr, lineage)
    fields = {
        "outcome": classification["outcome"],
        "closed_at": _ts(pr.get("merged_at") or pr.get("closed_at")) or time.time(),
        "closed_by": (pr.get("merged_by") or {}).get("login"),
        "corrected_spec": classification["corrected_spec"],
        "correction_source": classification["correction_source"],
        "notes": "; ".join(classification["notes"]) or None,
    }

    if make_candidates and _should_make_candidate(classification) and not (existing or {}).get("candidate_path"):
        candidate = build_candidate(pr, lineage, classification)
        try:
            path, url = open_candidate_pr(pr, lineage, candidate)
            fields["candidate_path"], fields["candidate_pr_url"] = path, url
        except GitHubPRError:
            logger.exception("Could not open eval-candidate PR for #%s", pr["number"])

    store.upsert_pr(pr["number"], **fields)
    refresh_cost(store.get_pr(pr["number"]))
    return f"closed -> {classification['outcome']}"


# ---------------------------------------------------------------- Langfuse cost

def fetch_langfuse_cost(trace_id: str) -> float | None:
    pk, sk = os.getenv("LANGFUSE_PUBLIC_KEY"), os.getenv("LANGFUSE_SECRET_KEY")
    base = os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com").rstrip("/")
    if not (pk and sk and trace_id):
        return None
    try:
        resp = requests.get(f"{base}/api/public/traces/{trace_id}", auth=(pk, sk), timeout=15)
    except requests.RequestException:
        logger.warning("Langfuse cost lookup failed for %s", trace_id, exc_info=True)
        return None
    if resp.status_code != 200:
        return None
    data = resp.json()
    cost = data.get("totalCost")
    if cost is None:  # older API shapes: sum observation costs
        cost = sum(
            (o.get("calculatedTotalCost") or o.get("totalCost") or 0) for o in data.get("observations", [])
        ) or None
    return float(cost) if cost is not None else None


def refresh_cost(row: dict | None) -> None:
    """Pulls the authoritative cost for one PR's trace from Langfuse. Langfuse reports
    0 for models it has no price for, so a 0 never overrides a non-zero local estimate."""
    if not row or not row.get("trace_id"):
        return
    cost = fetch_langfuse_cost(row["trace_id"])
    if cost is None:
        return
    if cost > 0 or not row.get("cost_usd"):
        store.upsert_pr(row["pr_number"], cost_usd=cost, cost_source="langfuse")


def start_cost_refresher() -> threading.Thread | None:
    interval = float(os.getenv("LANGFUSE_COST_REFRESH_SECONDS", "300"))
    if interval <= 0 or not os.getenv("LANGFUSE_PUBLIC_KEY"):
        return None

    def loop():
        while True:
            try:
                for row in store.prs_needing_cost(max_age_seconds=14 * 86400):
                    refresh_cost(row)
            except Exception:  # pragma: no cover
                logger.exception("Langfuse cost refresh failed")
            time.sleep(interval)

    t = threading.Thread(target=loop, name="langfuse-cost-refresher", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------- Prometheus

class AgentPRCollector(Collector):
    """Exposes all-time PR outcome stats from the persistent store on every scrape."""

    def collect(self):
        try:
            rows = store.all_prs()
        except Exception:  # pragma: no cover - never break /metrics
            logger.exception("Could not read agent PR store")
            rows = []

        prs = GaugeMetricFamily(
            "agent_prs",
            "Agent-opened PRs by outcome (all time, from the persistent store)",
            labels=["outcome", "prompt_version", "model"],
        )
        counts: dict[tuple, int] = {}
        for r in rows:
            key = (r.get("outcome") or "open", r.get("prompt_version") or "", r.get("model") or "")
            counts[key] = counts.get(key, 0) + 1
        for o in OUTCOMES:  # always emit every outcome so rate queries never divide by "no data"
            if not any(k[0] == o for k in counts):
                counts[(o, "", "")] = 0
        for (outcome, pv, model), n in sorted(counts.items()):
            prs.add_metric([outcome, pv, model], n)
        yield prs

        merged = [
            r["closed_at"] - r["created_at"]
            for r in rows
            if (r.get("outcome") or "").startswith("merged") and r.get("closed_at") and r.get("created_at")
        ]
        buckets, acc = [], 0
        for b in _TTM_BUCKETS:
            acc = sum(1 for v in merged if v <= b)
            buckets.append((str(float(b)), acc))
        buckets.append(("+Inf", len(merged)))
        ttm = HistogramMetricFamily(
            "agent_pr_time_to_merge_seconds",
            "Time from agent PR opened to merged (all time)",
        )
        ttm.add_metric([], buckets, sum_value=sum(merged))
        yield ttm

        costed = [r for r in rows if r.get("cost_usd") is not None]
        cost = SummaryMetricFamily(
            "agent_dashboard_cost_usd",
            "LLM cost per generated dashboard (Langfuse when available, else local estimate)",
            labels=["source"],
        )
        for source in ("langfuse", "estimate"):
            subset = [r for r in costed if r.get("cost_source") == source]
            cost.add_metric([source], count_value=len(subset), sum_value=sum(r["cost_usd"] for r in subset))
        yield cost

        cands = GaugeMetricFamily(
            "agent_eval_candidates", "Eval candidates generated from reviewer corrections", labels=["source"]
        )
        by_src: dict[str, int] = {"pr_spec_block": 0, "merged_dashboard": 0, "none": 0}
        for r in rows:
            if r.get("candidate_path"):
                key = r.get("correction_source") or "none"
                by_src[key] = by_src.get(key, 0) + 1
        for src, n in by_src.items():
            cands.add_metric([src], n)
        yield cands
