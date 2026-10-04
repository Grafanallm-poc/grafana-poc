#!/usr/bin/env python3
"""Posts the approval-request message to #github-notifier when a PR opens against
main. Used by .github/workflows/notify-slack-pr.yml. Reads PR fields from env vars
(set directly from the event payload, not interpolated into this script's source)
so a PR with no description, or special characters in its title, can't break it.
"""
from __future__ import annotations

import json
import os
import urllib.request

CHANNEL = "#github-notifier"


def main() -> None:
    title = os.environ["PR_TITLE"]
    url = os.environ["PR_URL"]
    author = os.environ["PR_AUTHOR"]

    text = (
        "Hi Team,\n"
        "New Dashboard Removal/Adding request is pending in our queue, "
        "please review it and provide your approval by adding your review on it.\n\n"
        f"*{title}*\n"
        f"Opened by: {author}\n"
        f"<{url}|Review and approve on GitHub>"
    )

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


if __name__ == "__main__":
    main()
