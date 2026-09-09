#!/usr/bin/env bash
# Deploy one Actor, and fail unless the build it created actually succeeded.
#
# `sync_actor_files.py` copies `core/` and `requirements.txt` into the build context —
# they are gitignored there, so skipping it fails the image on `"/requirements.txt": not
# found`, which is how build 0.1.7 died on 2026-09-09. And `apify push -w` returns 0 when
# the wait elapses with the build still running, so "the command came back clean" is not
# the same as "the build succeeded"; this reads the status of the build the push created.
#
#     scripts/deploy_actor.sh ats-jobs-lifecycle
set -euo pipefail
cd "$(dirname "$0")/.."
actor="${1:?usage: deploy_actor.sh <actor-dir-name>}"
[ -d "actors/$actor" ] || { echo "no such actor: actors/$actor" >&2; exit 2; }

py=.venv/bin/python
[ -x "$py" ] || py=python3

# Registered before the sync, not after: a sync that fails halfway still leaves `core/`
# beside `src/`, and that is the state `sync_actor_files.py` says stops `apify run`.
trap '"$py" scripts/sync_actor_files.py --clean "$actor" >/dev/null 2>&1 || true' EXIT
"$py" scripts/sync_actor_files.py "$actor"

pushed=$(cd "actors/$actor" && apify push -w 900 --json)
build=$(printf '%s' "$pushed" | python3 -c 'import sys,json;print((json.load(sys.stdin).get("build") or {}).get("id",""))')
[ -n "$build" ] || { echo "push returned no build id:"; printf '%s\n' "$pushed"; exit 1; }

# Keyed to this build, not to "the newest build of some hardcoded account". The push goes
# to whatever account `apify auth` holds, so reading a username's latest build can report
# SUCCEEDED for a deploy that landed somewhere else entirely.
status=$(curl -fsS --retry 3 --max-time 30 -H "Authorization: Bearer $(apify auth token)" \
  "https://api.apify.com/v2/actor-builds/$build" |
  python3 -c 'import sys,json;b=json.load(sys.stdin)["data"];print(b.get("buildNumber",""),b["status"])') \
  || { echo "could not read build $build; check it before assuming either outcome" >&2; exit 1; }

echo "build $status"
[ "${status##* }" = "SUCCEEDED" ]
