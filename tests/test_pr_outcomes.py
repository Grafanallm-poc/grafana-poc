"""End-to-end: /onboard opens a stamped PR -> webhook closes it -> outcome,
metrics and eval candidate are produced. GitHub is faked in-memory."""
import copy
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from prometheus_client import generate_latest

from agent import app as app_mod
from agent import github_pr, pr_outcomes, store
from agent.lineage import parse_marker, render_pr_body
from agent.llm.spec_extractor import ExtractionResult
from agent.schema import PartnerSpec

SECRET = "s3cret"


class FakeGitHub:
    def __init__(self):
        self.files = {}  # (ref, path) -> text
        self.prs = {}
        self.n = 41

    def open_pr(self, *, branch, files, commit_message, title, body, labels=None):
        self.n += 1
        sha = f"sha{self.n}"
        for p, c in files.items():
            self.files[(sha, p)] = c
            self.files[(branch, p)] = c
        pr = {"number": self.n, "html_url": f"https://github.com/acme/grafana-poc/pull/{self.n}",
              "body": body(sha) if callable(body) else body, "head": {"ref": branch, "sha": sha},
              "title": title, "labels": labels, "files": files, "created_at": "2026-10-07T10:00:00Z"}
        self.prs[self.n] = pr
        return {"pr_url": pr["html_url"], "number": self.n, "branch": branch, "commit_sha": sha}

    def get_file_text(self, path, ref):
        return self.files.get((ref, path))


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(github_pr, "open_pr", fake.open_pr)
    monkeypatch.setattr(pr_outcomes, "open_pr", fake.open_pr)
    monkeypatch.setattr(pr_outcomes, "get_file_text", fake.get_file_text)
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    return fake


@pytest.fixture
def client(monkeypatch, gh):
    spec = PartnerSpec.model_validate(
        {"partner_name": "Moloco", "partner_type": "DSP", "metrics": ["latency", "error_5xx"],
         "alerts": [{"metric": "latency", "aggregation": "p95", "operator": ">", "threshold": 120, "unit": "ms"}]}
    )
    monkeypatch.setattr(
        app_mod, "extract_spec_with_lineage",
        lambda text: ExtractionResult(spec=spec, model="gemini-3.5-flash", prompt_version="v2",
                                      trace_id="a" * 32, trace_url="https://lf/project/p/traces/" + "a" * 32,
                                      attempted_models=["gemini-3.5-flash"], prompt_tokens=100, completion_tokens=20),
    )
    return TestClient(app_mod.app)


def _deliver(client, event, payload, secret=SECRET):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhooks/github", content=body,
                       headers={"X-GitHub-Event": event, "X-Hub-Signature-256": sig,
                                "Content-Type": "application/json"})


def _onboard(client, gh):
    r = client.post("/onboard", data={"request_text": "Onboard DSP Moloco, track latency and 5XX, alert if p95 > 120ms."})
    assert r.status_code == 200 and "PR opened" in r.text and "Lineage" in r.text
    pr = gh.prs[max(gh.prs)]
    return pr


def _closed(pr, *, merged, head_sha=None, body=None):
    p = copy.deepcopy(pr)
    p.update(state="closed", merged=merged, closed_at="2026-10-07T11:30:00Z",
             merged_at="2026-10-07T11:30:00Z" if merged else None,
             merged_by={"login": "reviewer"} if merged else None)
    if head_sha:
        p["head"]["sha"] = head_sha
    if body is not None:
        p["body"] = body
    return p


def test_onboard_stamps_pr_and_dashboard(client, gh):
    pr = _onboard(client, gh)
    lineage = parse_marker(pr["body"])
    assert lineage["agent_commit_sha"] == pr["head"]["sha"]
    assert lineage["prompt_version"] == "v2" and lineage["model"] == "gemini-3.5-flash"
    dash = json.loads(pr["files"]["dashboards/moloco.json"])
    assert dash["spec"]["links"][0]["url"].endswith("a" * 32)
    assert "agent-generated" in pr["labels"]
    assert store.get_pr(pr["number"])["outcome"] == "open"


def test_bad_signature_rejected(client, gh):
    assert _deliver(client, "pull_request", {"action": "closed"}, secret="wrong").status_code == 401


def test_merged_unchanged(client, gh):
    pr = _onboard(client, gh)
    r = _deliver(client, "pull_request", {"action": "closed", "pull_request": _closed(pr, merged=True)})
    assert r.status_code == 202
    row = store.get_pr(pr["number"])
    assert row["outcome"] == "merged_unchanged"
    assert row["closed_at"] - row["created_at"] > 0
    assert row["candidate_path"] is None
    metrics = generate_latest().decode()
    assert 'agent_prs{model="gemini-3.5-flash",outcome="merged_unchanged",prompt_version="v2"} 1.0' in metrics
    assert "agent_pr_time_to_merge_seconds_count 1.0" in metrics


def test_merged_with_dashboard_edit_creates_candidate(client, gh):
    pr = _onboard(client, gh)
    dash = json.loads(pr["files"]["dashboards/moloco.json"])
    # reviewer: threshold should be 150ms, and they also want QPS... they delete 5XX panel
    dash["spec"]["panels"] = [p for p in dash["spec"]["panels"] if not p["title"].startswith("5XX")]
    dash["spec"]["panels"][0]["fieldConfig"]["defaults"]["thresholds"]["steps"][1]["value"] = 150
    gh.files[("reviewer-sha", "dashboards/moloco.json")] = json.dumps(dash)
    _deliver(client, "pull_request", {"action": "closed", "pull_request": _closed(pr, merged=True, head_sha="reviewer-sha")})

    row = store.get_pr(pr["number"])
    assert row["outcome"] == "merged_edited"
    assert row["correction_source"] == "merged_dashboard"
    assert row["corrected_spec"]["metrics"] == ["latency"]
    cand_pr = gh.prs[max(gh.prs)]
    assert cand_pr["labels"] == ["eval-candidate"]
    (path, content), = cand_pr["files"].items()
    assert path == f"evals/candidates/moloco-pr{pr['number']}.json"
    cand = json.loads(content)
    assert cand["request"].startswith("Onboard DSP Moloco")
    assert cand["extracted"]["metrics"] == ["latency", "error_5xx"]
    assert cand["corrected"]["alerts"][0]["threshold"] == 150
    assert cand["lineage"]["trace_id"] == "a" * 32
    # the candidate PR itself must be ignored by outcome tracking
    assert pr_outcomes.handle_pull_request(_closed(cand_pr, merged=True), "closed").startswith("ignored")


def test_layout_only_edit_is_not_a_candidate(client, gh):
    pr = _onboard(client, gh)
    dash = json.loads(pr["files"]["dashboards/moloco.json"])
    dash["spec"]["refresh"] = "1m"
    gh.files[("sha-x", "dashboards/moloco.json")] = json.dumps(dash)
    _deliver(client, "pull_request", {"action": "closed", "pull_request": _closed(pr, merged=True, head_sha="sha-x")})
    row = store.get_pr(pr["number"])
    assert row["outcome"] == "merged_edited" and row["candidate_path"] is None
    assert "template/layout" in row["notes"]


def test_spec_block_edit_wins(client, gh):
    pr = _onboard(client, gh)
    lineage = parse_marker(pr["body"])
    edited = copy.deepcopy(lineage)
    edited["extracted_spec"]["partner_type"] = "SSP"  # reviewer: it's an SSP!
    body = render_pr_body(edited).split("<!-- agent-lineage")[0] + pr["body"][pr["body"].index("<!-- agent-lineage"):]
    _deliver(client, "pull_request", {"action": "closed", "pull_request": _closed(pr, merged=False, body=body)})
    row = store.get_pr(pr["number"])
    assert row["outcome"] == "closed_unmerged"
    assert row["correction_source"] == "pr_spec_block"
    assert row["corrected_spec"]["partner_type"] == "SSP"


def test_closed_without_fix_creates_empty_candidate_and_is_idempotent(client, gh):
    pr = _onboard(client, gh)
    payload = {"action": "closed", "pull_request": _closed(pr, merged=False)}
    _deliver(client, "pull_request", payload)
    n = len(gh.prs)
    _deliver(client, "pull_request", payload)  # GitHub redelivery
    assert len(gh.prs) == n
    cand = json.loads(next(iter(gh.prs[max(gh.prs)]["files"].values())))
    assert cand["corrected"] is None
    m = generate_latest().decode()
    assert 'agent_prs{model="gemini-3.5-flash",outcome="closed_unmerged",prompt_version="v2"} 1.0' in m


def test_ping_and_non_agent_pr(client, gh):
    assert _deliver(client, "ping", {"zen": "hi"}).json()["pong"]
    assert pr_outcomes.handle_pull_request({"number": 1, "body": "human PR"}, "closed").startswith("ignored")


def test_langfuse_cost_preferred(client, gh, monkeypatch):
    pr = _onboard(client, gh)
    monkeypatch.setattr(pr_outcomes, "fetch_langfuse_cost", lambda tid: 0.0123)
    pr_outcomes.refresh_cost(store.get_pr(pr["number"]))
    row = store.get_pr(pr["number"])
    assert row["cost_source"] == "langfuse" and row["cost_usd"] == pytest.approx(0.0123)
    assert 'agent_dashboard_cost_usd_sum{source="langfuse"} 0.0123' in generate_latest().decode()
