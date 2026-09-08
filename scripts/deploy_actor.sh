#!/usr/bin/env bash
# Deploy one Actor, and fail when the build fails.
#
# Two steps are easy to skip and both have cost a deploy already: `sync_actor_files.py`
# puts `core/` and `requirements.txt` into the build context (they are gitignored there),
# and `apify push` exits 0 even when the container build fails, so the status has to be
# read back from the API.
#
#     scripts/deploy_actor.sh ats-jobs-lifecycle
set -euo pipefail
cd "$(dirname "$0")/.."
actor="${1:?usage: deploy_actor.sh <actor-dir-name>}"

python scripts/sync_actor_files.py "$actor"
trap 'python scripts/sync_actor_files.py --clean "$actor" >/dev/null' EXIT
(cd "actors/$actor" && apify push -w 900)

token="$(apify auth token)"
read -r number status < <(
  curl -fsS -H "Authorization: Bearer $token" \
    "https://api.apify.com/v2/acts/acotr_moonie~$actor/builds?limit=1&desc=1" |
  python3 -c 'import sys,json;b=json.load(sys.stdin)["data"]["items"][0];print(b["buildNumber"],b["status"])'
)
echo "build $number $status"
[ "$status" = "SUCCEEDED" ]
