"""Checks for the Actor shell itself: input handling, record shaping, free row shapes.

The pipeline's shared logic is covered where it lives (`test_filters.py`,
`test_billing.py`, `test_state.py`); this file locks down what only `core/run.py` does —
competitor key aliases, input defaults, the output switches, and the dataset keys the
free `company_summary` / `error` rows emit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.models import JobRecord, Ref
from core.providers import AdapterNotFound, get_adapter
from core.run import (
    NUMERIC_BOUNDS,
    RunCtx,
    dedupe,
    error_item,
    read_companies,
    read_config,
    shape_record,
    summary_item,
    top_departments,
)
from core.state import SeenState

ROOT = Path(__file__).resolve().parent.parent
ACTOR = ROOT / "actors" / "ats-jobs-scraper"

DATASET_FIELDS = set(
    json.loads((ACTOR / ".actor" / "dataset_schema.json").read_text(encoding="utf-8"))["fields"][
        "properties"
    ]
)


def job(job_id: str = "greenhouse:acme:1", **kwargs) -> JobRecord:
    return JobRecord(id=job_id, title="Senior Backend Engineer", **kwargs)


def test_companies_accept_competitor_input_keys():
    assert read_companies({"companies": ["lever:palantir"], "queries": ["x"]}) == ["lever:palantir"]
    assert read_companies({"boardTokens": ["anthropic", "anthropic"]}) == ["anthropic"]
    assert read_companies({"startUrls": [{"url": "https://jobs.ashbyhq.com/OpenAI"}]}) == [
        "https://jobs.ashbyhq.com/OpenAI"
    ]
    assert read_companies({}) == []


def test_config_keeps_meaningful_falsy_values():
    cfg = read_config({"maxJobs": 0, "includeCompanySummary": False, "providers": ["lever", "x"]})
    assert cfg["maxJobs"] == 0, "0 means no limit, not 'use the default'"
    assert cfg["includeCompanySummary"] is False
    assert cfg["providers"] == ["lever"]
    assert read_config({})["maxJobs"] == 1000


def test_descriptions_ship_by_default_and_raw_does_not():
    """`includeDescription` flipped to default `true`: every provider returns the body in
    the response we already make, billing is per row either way, so the buyer gets it.
    `includeRawJson` stays opt-in — it is bulk, not value."""
    record = job(descriptionHtml="<p>hi</p>", descriptionText="hi", raw={"a": 1})
    shape_record(record, read_config({}))
    assert record.descriptionText == "hi", "descriptionFormat still defaults to 'text'"
    assert record.descriptionHtml is None
    assert record.raw is None and record.descriptionRedacted is False

    record = job(descriptionHtml="<p>hi</p>", descriptionText="hi", raw={"a": 1})
    shape_record(record, read_config({"includeDescription": False}))
    assert record.descriptionHtml is None and record.descriptionText is None
    assert record.descriptionRedacted is None

    record = job(descriptionHtml="<p>hi</p>", descriptionText="hi", raw={"a": 1})
    shape_record(record, read_config({"includeDescription": True, "includeRawJson": True}))
    assert record.descriptionText == "hi", "descriptionFormat 'text' keeps the text"
    assert record.descriptionHtml is None
    assert record.raw == {"a": 1}


def test_contacts_are_redacted_by_default_and_flagged():
    record = job(descriptionText="Questions? mail jane.doe@acme.com")
    shape_record(record, read_config({"includeDescription": True}))
    assert "jane.doe@acme.com" not in (record.descriptionText or "")
    assert record.descriptionRedacted is True

    record = job(descriptionText="Questions? mail jane.doe@acme.com")
    shape_record(record, read_config({"includeDescription": True, "redactContacts": False}))
    assert "jane.doe@acme.com" in record.descriptionText


def _ctx(dedupe_mode: str = "id") -> RunCtx:
    ctx = RunCtx.__new__(RunCtx)  # no Actor, no Billing needed for the dedupe pass
    ctx.cfg = read_config({"dedupe": dedupe_mode})
    ctx.seen_ids = set()
    ctx.content_survivors = {}
    return ctx


def test_dedupe_by_id_is_always_on_and_run_wide():
    ctx = _ctx()
    first, dropped = dedupe(ctx, [job("a"), job("b")])
    assert [r.id for r in first] == ["a", "b"] and dropped == 0
    second, dropped = dedupe(ctx, [job("b"), job("c")])
    assert [r.id for r in second] == ["c"] and dropped == 1


def test_content_dedupe_records_what_it_swallowed():
    ctx = _ctx("content")
    kept, dropped = dedupe(
        ctx, [job("a", contentKey="k"), job("b", contentKey="k"), job("c", contentKey="j")]
    )
    assert [r.id for r in kept] == ["a", "c"]
    assert dropped == 1
    assert kept[0].dedupedFrom == ["b"], "§4.5.6: the loss is visible on the surviving row"


def test_top_departments_counts_the_kept_rows():
    records = [job("a", department="Engineering"), job("b", department="Engineering"), job("c")]
    assert top_departments(records) == [{"name": "Engineering", "count": 2}]
    assert top_departments([job("a")]) is None


def test_free_rows_only_use_declared_dataset_fields():
    ref = Ref(provider="greenhouse", slug="anthropic", input="anthropic")
    summary = summary_item(ref, status="ok", jobs_found=533, jobs_kept=37, new_jobs=3)
    error = error_item("lever", "nope", "lever:nope", "not_found", "404 from api.lever.co")
    for row in (summary, error):
        undeclared = set(row) - DATASET_FIELDS
        assert not undeclared, f"undeclared dataset fields: {sorted(undeclared)}"
    assert summary["recordType"] == "company_summary"
    assert error["recordType"] == "error" and error["status"] == "not_found"


def test_missing_adapter_raises_a_typed_error():
    with pytest.raises(AdapterNotFound):
        get_adapter("smartrecruiters")


# --- V1 H2/H3/H4 + V3 S22: the numeric knobs an API caller reaches unfiltered ----------


@pytest.mark.parametrize("bad", ["lots", "1e9", "0x10", -5, 10**9, 10**400, 30.0, "30", {"x": 1}])
def test_numeric_input_never_raises_and_never_leaves_the_schema_bounds(bad):
    """V3 S22: the schema's `minimum`/`maximum` bind the Console form only — API, CLI and
    `call-actor` callers reach `Actor.get_input()` without it. `companies.maxItems` already
    had `MAX_COMPANIES` for exactly this reason; the five knobs beside it were undefended,
    and `int("lots")` failed the run before a company was resolved."""
    cfg = read_config(dict.fromkeys(NUMERIC_BOUNDS, bad))
    for key, (low, high) in NUMERIC_BOUNDS.items():
        assert isinstance(cfg[key], int) and low <= cfg[key] <= high, key


def test_a_negative_retention_cannot_wipe_the_delta_baseline():
    """V1 H2: `prune(-1)` put the cutoff a day in the *future*, so every id was stale and
    `save()` persisted the empty dict — the next run saw no baseline, called every job new
    and charged $0.002 apiece."""
    assert read_config({"stateRetentionDays": -1})["stateRetentionDays"] == 0
    state = SeenState()
    state.mark("a")
    state.mark("b")
    assert state.prune(-1) == 0 and len(state.seen) == 2
    assert state.prune(10**9) == 0, "V3 S21: a huge retention used to overflow timedelta"


def test_a_negative_per_company_cap_cannot_silently_drop_rows():
    """V1 H3: `-5` is truthy, so `emit[: cfg["maxJobsPerCompany"]]` became `emit[:-5]` and
    discarded the last five rows the buyer asked for, while the summary still reported
    them as kept."""
    assert read_config({"maxJobsPerCompany": -5})["maxJobsPerCompany"] == 0


def test_a_zero_timeout_cannot_fail_every_request():
    """V1 H4: `httpx.Timeout(0.0)` sets connect/read/write/pool to zero, so every fetch
    raised instantly, every company became a free `timeout` row, and the run still exited
    SUCCEEDED with zero jobs."""
    assert read_config({"requestTimeoutSecs": 0})["requestTimeoutSecs"] == 5


def test_a_non_list_companies_value_is_no_companies():
    """V3 S30: a dict iterated as its *keys* — an input shape nobody meant to support."""
    assert read_companies({"companies": {"a": 1}}) == []
    assert read_companies({"companies": "lever:palantir"}) == ["lever:palantir"]


# --- V1 M6: the takedown switch §14.3 already documents -------------------------------


def test_a_disabled_provider_becomes_a_free_error_row_not_a_crash(monkeypatch):
    """V1 M6: §14.3 step 2 specifies the runbook — "set the adapter's status to degraded;
    the Actor then emits `error` rows with `provider_unavailable`, still succeeds, and
    still bills nothing for it". No such switch existed, so honouring a takedown inside the
    48 hours §15.1 policy 4 promises meant deleting a module and rebuilding."""
    import core.providers as providers

    assert providers.DISABLED == frozenset(), "nothing is disabled in a normal build"
    monkeypatch.setattr(providers, "DISABLED", frozenset({"lever"}))
    with pytest.raises(AdapterNotFound):
        get_adapter("lever")
    assert get_adapter("greenhouse") is not None, "the other five keep working"
