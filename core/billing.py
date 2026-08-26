"""Charge policy (SPEC v2 §8.2, §5.12).

One paid event: `job`, charged as each surviving row is pushed. `company_summary` and
`error` rows are pushed free. `delta-run` is charged once per run when `onlyNewJobs` is
on and is priced at $0.00 — instrumentation, not revenue (§8.2).

Nothing else charges, ever. `apify-actor-start` is automatic and `apify-default-dataset-item`
is deleted in the Console, otherwise every job would be double-charged (§8.1).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

JOB_EVENT = "job"
DELTA_RUN_EVENT = "delta-run"

#: §5.12 statuses a stopped run writes onto the remaining companies' free summary rows.
MAX_JOBS_REACHED = "max_jobs_reached"
BUDGET_EXHAUSTED = "budget_exhausted"


class Billing:
    """Owns every `push_data` call, so "charge only for delivered value" (G5) is one
    place in the code rather than a rule every caller has to remember."""

    def __init__(self, *, max_jobs: int = 0, actor: Any | None = None):
        if actor is None:
            from apify import Actor

            actor = Actor
        self._actor = actor
        self.max_jobs = max(0, int(max_jobs or 0))
        self.jobs_pushed = 0
        self.jobs_charged = 0
        self.free_pushed = 0
        self.max_jobs_reached = False
        self.budget_exhausted = False
        self.delta_run_charged = False

    @property
    def stopped(self) -> bool:
        """True once no further job row may be pushed."""
        return self.max_jobs_reached or self.budget_exhausted

    @property
    def stop_status(self) -> str | None:
        """The status for the free summary rows of companies never reached (§5.12)."""
        if self.budget_exhausted:
            return BUDGET_EXHAUSTED
        if self.max_jobs_reached:
            return MAX_JOBS_REACHED
        return None

    @property
    def remaining(self) -> int | None:
        """Job rows still allowed by `maxJobs`. None = no limit."""
        if not self.max_jobs:
            return None
        return max(0, self.max_jobs - self.jobs_pushed)

    async def push_job(self, item: dict[str, Any]) -> bool:
        """Push one job row and charge `job` for it. Returns whether it was delivered.

        `maxJobs` is checked **before** the push so the cap is exact and companies past
        it are never touched, let alone charged (§5.12, V2 T-C5).
        """
        if self.budget_exhausted:
            return False
        if self.max_jobs and self.jobs_pushed >= self.max_jobs:
            self.max_jobs_reached = True
            return False

        result = await self._actor.push_data(item, charged_event_name=JOB_EVENT)
        charged = getattr(result, "charged_count", 0) or 0
        limit_reached = bool(getattr(result, "event_charge_limit_reached", False))

        # The SDK pushes only what it can charge, so "limit reached and nothing charged"
        # is the one case where the row did not land. On a non-PPE run charged_count is
        # 0 and the limit is never reached, so the row did land.
        if limit_reached and charged == 0:
            self.budget_exhausted = True
            logger.info("ACTOR_MAX_TOTAL_CHARGE_USD reached; no further job rows charged")
            return False

        self.jobs_pushed += 1
        self.jobs_charged += charged
        if limit_reached:
            self.budget_exhausted = True
            logger.info("ACTOR_MAX_TOTAL_CHARGE_USD reached after %d rows", self.jobs_pushed)
        elif self.max_jobs and self.jobs_pushed >= self.max_jobs:
            self.max_jobs_reached = True
        return True

    async def push_free(self, item: dict[str, Any]) -> None:
        """`company_summary` and `error` rows. Never charged, pushed even when stopped."""
        await self._actor.push_data(item)
        self.free_pushed += 1

    async def charge_delta_run(self) -> None:
        """Once per run when `onlyNewJobs` is on (§8.2). $0.00 — a usage counter."""
        if self.delta_run_charged:
            return
        self.delta_run_charged = True
        try:
            await self._actor.charge(DELTA_RUN_EVENT)
        except Exception as exc:
            # A missing instrumentation event must never fail a paid run.
            logger.info("could not record %s: %s", DELTA_RUN_EVENT, exc)
