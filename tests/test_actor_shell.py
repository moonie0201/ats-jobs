"""Checks for the Actor shell itself: input handling, record shaping, free row shapes.

The pipeline's shared logic is covered where it lives (`test_filters.py`,
`test_billing.py`, `test_state.py`); this file locks down what only `src/main.py` does —
competitor key aliases, input defaults, the output switches, and the dataset keys the
free `company_summary` / `error` rows emit.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ACTOR = ROOT / "actors" / "ats-jobs-scraper"
# Appended, never prepended: `scripts/sync_actor_files.py` drops a build copy of `core/`
# into the Actor directory, and prepending would shadow the real one under test (V1 B2).
sys.path.append(str(ACTOR))

from src.main import (  # noqa: E402
    RunCtx,
    dedupe,
    error_item,
    read_companies,
    read_config,
    shape_record,
    summary_item,
    top_departments,
)

from core.models import JobRecord, Ref  # noqa: E402
from core.providers import AdapterNotFound, get_adapter  # noqa: E402

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


def test_descriptions_and_raw_are_dropped_unless_asked_for():
    record = job(descriptionHtml="<p>hi</p>", descriptionText="hi", raw={"a": 1})
    shape_record(record, read_config({}))
    assert record.descriptionHtml is None and record.descriptionText is None
    assert record.raw is None and record.descriptionRedacted is None

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
