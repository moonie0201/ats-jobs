#!/usr/bin/env bash
# Daily KPI snapshot -> ../spec/kpi_log.md  (cron: 0 9 * * *)
# One block per public Actor (ats-jobs + apify-utils); KPI_ACTOR selects the Actor in kpi.py.
set -euo pipefail
cd "$(dirname "$0")/.."
export APIFY_TOKEN="${APIFY_TOKEN:-$(apify auth token)}"
ACTORS="aZfd1nEuYfHNDz2mt RB7nRHgOZUt8vfQ0J YF4WQE3mjgmvH4VKf 9bO5UGoeYbVqNjlNg IvKrXhrYi0ssivDOu j2Hhchj8IFP2gszxa"
{ echo; echo "## $(date -u +%F)"; for a in $ACTORS; do echo '```'; KPI_ACTOR="$a" .venv/bin/python scripts/kpi.py || echo "kpi.py failed for $a"; echo '```'; done; } >> ../spec/kpi_log.md
