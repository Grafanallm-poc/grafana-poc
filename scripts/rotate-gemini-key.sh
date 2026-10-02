#!/usr/bin/env bash
# Rotates GEMINI_API_KEY everywhere this deployment uses it:
#   1. The live agent's .env on EC2 — then RECREATES the onboarding-agent container,
#      because `docker restart` does NOT reload --env-file; only `docker run` does.
#      A plain restart after editing .env would silently keep using the old key.
#   2. The GEMINI_API_KEY GitHub Actions secret (used by .github/workflows/eval-gate.yml)
#
# Usage:
#   ./scripts/rotate-gemini-key.sh [new-key]
#   If you omit the argument, you'll be prompted with hidden input instead — safer,
#   since a key passed as an argument lands in your local shell history.
#
# Required environment variables:
#   EC2_SSH_KEY_PATH   path to the .pem file for the EC2 instance
#   GITHUB_TOKEN       a GitHub PAT with repo + actions:write scope, to update the
#                       Actions secret. If unset, that step is skipped with a warning.
#
# Optional (defaults match this POC's current deployment):
#   EC2_HOST           default: ubuntu@ec2-3-108-197-35.ap-south-1.compute.amazonaws.com
#   GITHUB_REPO        default: Grafanallm-poc/grafana-poc
#   REMOTE_ENV_PATH    default: /home/ubuntu/grafana-poc/.env
#
# The key is never passed as a command-line argument to ssh/python/docker, so it
# won't show up in `ps` output on either machine — it's piped over stdin / passed as
# a scoped environment variable to the one process that needs it.

set -euo pipefail

EC2_HOST="${EC2_HOST:-ubuntu@ec2-3-108-197-35.ap-south-1.compute.amazonaws.com}"
GITHUB_REPO="${GITHUB_REPO:-Grafanallm-poc/grafana-poc}"
REMOTE_ENV_PATH="${REMOTE_ENV_PATH:-/home/ubuntu/grafana-poc/.env}"

if [[ -z "${EC2_SSH_KEY_PATH:-}" ]]; then
  echo "ERROR: set EC2_SSH_KEY_PATH to your EC2 .pem file path." >&2
  exit 1
fi

NEW_KEY="${1:-}"
if [[ -z "$NEW_KEY" ]]; then
  read -rsp "New Gemini API key: " NEW_KEY
  echo
fi
if [[ -z "$NEW_KEY" ]]; then
  echo "ERROR: no key provided." >&2
  exit 1
fi

SSH=(ssh -i "$EC2_SSH_KEY_PATH" "$EC2_HOST")

echo "==> Fetching current remote .env..."
CURRENT_ENV="$("${SSH[@]}" "cat $REMOTE_ENV_PATH")"

echo "==> Updating GEMINI_API_KEY in .env (in memory)..."
UPDATED_ENV="$(GEMINI_NEW_KEY="$NEW_KEY" python3 -c '
import os, sys
new_key = os.environ["GEMINI_NEW_KEY"]
lines = sys.stdin.read().splitlines()
found = False
out = []
for line in lines:
    if line.startswith("GEMINI_API_KEY="):
        out.append(f"GEMINI_API_KEY={new_key}")
        found = True
    else:
        out.append(line)
if not found:
    out.append(f"GEMINI_API_KEY={new_key}")
print("\n".join(out))
' <<< "$CURRENT_ENV")"

echo "==> Writing updated .env back to EC2..."
"${SSH[@]}" "cat > $REMOTE_ENV_PATH" <<< "$UPDATED_ENV"
"${SSH[@]}" "chmod 600 $REMOTE_ENV_PATH"

echo "==> Recreating onboarding-agent container so it picks up the new key..."
"${SSH[@]}" "
  docker rm -f onboarding-agent >/dev/null 2>&1 || true
  docker run -d --name onboarding-agent --restart unless-stopped \
    --network monitoring_monitoring -p 8000:8000 \
    --env-file $REMOTE_ENV_PATH onboarding-agent:latest
"

if [[ -z "${GITHUB_TOKEN:-}" ]]; then
  echo "==> GITHUB_TOKEN not set — skipping the GitHub Actions secret update." >&2
  echo "    Set GITHUB_TOKEN and re-run, or update it manually in repo Settings > Secrets." >&2
else
  echo "==> Updating GEMINI_API_KEY GitHub Actions secret on $GITHUB_REPO..."
  GEMINI_NEW_KEY="$NEW_KEY" GH_TOKEN="$GITHUB_TOKEN" GH_REPO="$GITHUB_REPO" python3 -c '
import os, base64, sys
try:
    import requests
    from nacl import encoding, public
except ImportError:
    sys.exit("Missing dependency — run: pip install requests pynacl")

token = os.environ["GH_TOKEN"]
repo = os.environ["GH_REPO"]
secret_value = os.environ["GEMINI_NEW_KEY"]
headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

pk = requests.get(f"https://api.github.com/repos/{repo}/actions/secrets/public-key", headers=headers).json()
public_key = public.PublicKey(pk["key"].encode(), encoding.Base64Encoder())
sealed_box = public.SealedBox(public_key)
encrypted = sealed_box.encrypt(secret_value.encode())
encrypted_b64 = base64.b64encode(encrypted).decode()

resp = requests.put(
    f"https://api.github.com/repos/{repo}/actions/secrets/GEMINI_API_KEY",
    headers=headers,
    json={"encrypted_value": encrypted_b64, "key_id": pk["key_id"]},
)
resp.raise_for_status()
print(f"GitHub secret updated: HTTP {resp.status_code}")
'
fi

echo "==> Verifying the agent is up..."
sleep 3
"${SSH[@]}" "curl -s -o /dev/null -w 'agent /healthz -> HTTP %{http_code}\n' http://localhost:8000/healthz"

echo
echo "Done. dummy-exporter / Grafana / Prometheus don't use this key, so they were left untouched."
echo "Once you've confirmed the new key works, revoke the old one in Google AI Studio."
