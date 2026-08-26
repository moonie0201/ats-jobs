"""§8.2 charge policy and §5.12's two stop conditions. One paid event: `job`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from core.billing import BUDGET_EXHAUSTED, MAX_JOBS_REACHED, Billing


@dataclass
class Result:
    event_charge_limit_reached: bool = False
    charged_count: int = 1
    chargeable_within_limit: dict[str, int | None] = field(default_factory=dict)


class FakeActor:
    """Stands in for `apify.Actor`; `results` replays the SDK's ChargeResult sequence."""

    def __init__(self, results: list[Result] | None = None):
        self.pushed: list[tuple[dict[str, Any], str | None]] = []
        self.charges: list[str] = []
        self._results = list(results or [])

    async def push_data(self, data, *, charged_event_name=None):
        result = self._results.pop(0) if self._results else Result()
        if charged_event_name and result.event_charge_limit_reached and result.charged_count == 0:
            return result  # the SDK pushes only what it can charge
        self.pushed.append((data, charged_event_name))
        return result

    async def charge(self, event_name, *, count=1):
        self.charges.append(event_name)
        return Result()

    @property
    def charged_rows(self):
        return [item for item, event in self.pushed if event == "job"]

    @property
    def free_rows(self):
        return [item for item, event in self.pushed if event is None]


def row(n: int) -> dict[str, Any]:
    return {"recordType": "job", "id": f"greenhouse:anthropic:{n}"}


async def test_a_job_row_is_pushed_and_charged_as_job():
    actor = FakeActor()
    billing = Billing(actor=actor)
    assert await billing.push_job(row(1)) is True
    assert actor.pushed == [(row(1), "job")]
    assert billing.jobs_pushed == 1
    assert billing.jobs_charged == 1


async def test_summary_and_error_rows_are_free():
    actor = FakeActor()
    billing = Billing(actor=actor)
    await billing.push_free({"recordType": "company_summary"})
    await billing.push_free({"recordType": "error"})
    assert actor.charged_rows == []
    assert len(actor.free_rows) == 2
    assert billing.free_pushed == 2


async def test_max_jobs_stops_before_the_push_so_the_cap_is_exact():
    actor = FakeActor()
    billing = Billing(max_jobs=2, actor=actor)
    assert [await billing.push_job(row(n)) for n in range(3)] == [True, True, False]
    assert len(actor.charged_rows) == 2
    assert billing.max_jobs_reached
    assert billing.stopped
    assert billing.stop_status == MAX_JOBS_REACHED


async def test_companies_never_reached_are_charged_nothing():
    """§5.12 (V2 T-C5): the remaining companies get a *free* summary row, not a bill."""
    actor = FakeActor()
    billing = Billing(max_jobs=1, actor=actor)
    await billing.push_job(row(1))
    assert await billing.push_job(row(2)) is False
    await billing.push_free({"recordType": "company_summary", "status": billing.stop_status})
    assert len(actor.charged_rows) == 1
    assert actor.free_rows[0]["status"] == MAX_JOBS_REACHED


async def test_zero_max_jobs_means_no_limit():
    actor = FakeActor()
    billing = Billing(max_jobs=0, actor=actor)
    for n in range(5):
        assert await billing.push_job(row(n)) is True
    assert billing.remaining is None
    assert not billing.stopped


async def test_remaining_counts_down():
    billing = Billing(max_jobs=3, actor=FakeActor())
    assert billing.remaining == 3
    await billing.push_job(row(1))
    assert billing.remaining == 2


async def test_budget_limit_stops_pushing_immediately():
    """ACTOR_MAX_TOTAL_CHARGE_USD reached: stop, then write free summary rows (§5.12)."""
    actor = FakeActor([Result(), Result(event_charge_limit_reached=True, charged_count=0)])
    billing = Billing(actor=actor)
    assert await billing.push_job(row(1)) is True
    assert await billing.push_job(row(2)) is False
    assert billing.budget_exhausted
    assert billing.stop_status == BUDGET_EXHAUSTED
    assert len(actor.charged_rows) == 1

    # Further job rows are refused without even asking the platform.
    assert await billing.push_job(row(3)) is False
    assert len(actor.pushed) == 1

    # Free rows still go out.
    await billing.push_free({"recordType": "company_summary", "status": billing.stop_status})
    assert actor.free_rows[0]["status"] == BUDGET_EXHAUSTED


async def test_the_last_chargeable_row_is_still_delivered():
    actor = FakeActor([Result(event_charge_limit_reached=True, charged_count=1)])
    billing = Billing(actor=actor)
    assert await billing.push_job(row(1)) is True
    assert billing.budget_exhausted
    assert len(actor.charged_rows) == 1


async def test_budget_wins_over_max_jobs_in_the_summary_status():
    actor = FakeActor([Result(event_charge_limit_reached=True, charged_count=1)])
    billing = Billing(max_jobs=1, actor=actor)
    await billing.push_job(row(1))
    assert billing.max_jobs_reached is False
    assert billing.stop_status == BUDGET_EXHAUSTED


async def test_a_non_ppe_run_still_delivers_rows():
    """Locally (and on non-PPE runs) charged_count is 0 and no limit is ever reached —
    that must not read as "the row did not land"."""
    actor = FakeActor([Result(charged_count=0), Result(charged_count=0)])
    billing = Billing(actor=actor)
    assert await billing.push_job(row(1)) is True
    assert await billing.push_job(row(2)) is True
    assert billing.jobs_pushed == 2
    assert billing.jobs_charged == 0


async def test_delta_run_is_charged_once_per_run():
    actor = FakeActor()
    billing = Billing(actor=actor)
    await billing.charge_delta_run()
    await billing.charge_delta_run()
    assert actor.charges == ["delta-run"]


async def test_delta_run_failure_never_breaks_the_run():
    class Broken(FakeActor):
        async def charge(self, event_name, *, count=1):
            raise RuntimeError("no pricing info")

    billing = Billing(actor=Broken())
    await billing.charge_delta_run()
    assert billing.delta_run_charged


async def test_nothing_else_is_ever_charged():
    actor = FakeActor()
    billing = Billing(actor=actor)
    await billing.push_job(row(1))
    await billing.push_free({"recordType": "error"})
    assert {event for _, event in actor.pushed} == {"job", None}
    assert actor.charges == []


def test_negative_max_jobs_is_clamped():
    assert Billing(max_jobs=-5, actor=FakeActor()).max_jobs == 0


@pytest.mark.parametrize("value", [None, 0])
def test_falsey_max_jobs_means_unlimited(value):
    assert Billing(max_jobs=value, actor=FakeActor()).remaining is None
