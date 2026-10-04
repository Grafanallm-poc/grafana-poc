#!/usr/bin/env python3
"""Posts a Slack message for every dashboard added or removed in a push to main.

Used by .github/workflows/notify-slack-dashboard-change.yml. Diffs
$BEFORE_SHA..$AFTER_SHA under dashboards/ and announces each addition/removal to
#dsp-ssp-dashboards, including dashboards removed by Grafana's own Git Sync
auto-commits (e.g. when someone deletes a dashboard via the Grafana UI) as well as
ones added through the onboarding agent's PR flow.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request

CHANNEL = "#dsp-ssp-dashboards"
ZERO_SHA = "0" * 40
_ONBOARDING_TITLE_RE = re.compile(r"^(SSP|DSP) Onboarding - (.+)$")


def dashboard_title(ref: str, path: str) -> str:
    try:
        content = subprocess.run(
            ["git", "show", f"{ref}:{path}"], capture_output=True, text=True, check=True
        ).stdout
        data = json.loads(content)
        spec = data.get("spec", data)
        return spec.get("title", path)
    except Exception:
        return path


def short_label(title: str) -> str:
    """"DSP Onboarding - DT5" -> "DSP DT5"; anything else (hand-built dashboards
    like the Kubernetes one) is left as-is.
    """
    m = _ONBOARDING_TITLE_RE.match(title)
    return f"{m.group(1)} {m.group(2)}" if m else title


def post_to_slack(text: str) -> None:
    token = os.environ["SLACK_BOT_TOKEN"]
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps({"channel": CHANNEL, "text": text}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
        if not result.get("ok"):
            print(f"Slack post failed: {result}")


def main() -> None:
    before = os.environ["BEFORE_SHA"]
    after = os.environ["AFTER_SHA"]
    if before == ZERO_SHA:
        print("Initial push to branch; skipping diff.")
        return

    diff = subprocess.run(
        ["git", "diff", "--name-status", before, after, "--", "dashboards/"],
        capture_output=True, text=True, check=True,
    ).stdout

    for line in diff.splitlines():
        if not line.strip():
            continue
        status, path = line.split("\t", 1)
        if not path.endswith(".json"):
            continue

        if status == "A":
            label = short_label(dashboard_title(after, path))
            post_to_slack(f"Hi, New Dashboard for {label} is added on Grafana.")
        elif status == "D":
            label = short_label(dashboard_title(before, path))
            post_to_slack(f"Hi, Dashboard for {label} is removed from Grafana.")


if __name__ == "__main__":
    main()
