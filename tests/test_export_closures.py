"""`scripts/export_closures.py`: the free-tier field gate and the §7.4 closure gates.

Loaded by path, like `test_snapshot.py` loads the Actor shell: `scripts/` is not a package
and importing it by name would depend on which directory pytest was started from.

The field test is the one that matters. Everything else in this repo can be wrong and cost
a rebuild; a job title, a URL or an ad body in the free file is CC0 and irrevocable.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from pathlib import Path

from core.diff import EVENT_KEYS

ROOT = Path(__file__).resolve().parent.parent


def _load():
    path = ROOT / "scripts" / "export_closures.py"
    spec = importlib.util.spec_from_file_location("export_closures", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ex = _load()

D1, D2, D3, D4 = "2026-08-26", "2026-08-27", "2026-08-28", "2026-08-29"


def count(day, provider, company, open_):
    return {"d": day, "provider": provider, "company": company, "open": open_}


def event(day, provider, company, ev, job_id="j1", **over):
    """A full 13-key event carrying exactly the free text and identifiers that must never
    reach the free tier — the test is worthless with placeholder values."""
    return {
        "d": day,
        "provider": provider,
        "company": company,
        "job_id": job_id,
        "ev": ev,
        "t": "Executive Assistant to the Founder",
        "loc": "12 Privet Drive, Little Whinging",
        "dept": "Engineering",
        "url": f"https://jobs.ashbyhq.com/{company}/{job_id}",
        "posted": "2026-05-21",
        "days_open": 99,
        "changed": None,
        "verified": None,
        **over,
    }


# ------------------------------------------------------------------ the field gate


def test_free_sample_carries_only_the_six_allowed_fields():
    counts = [count(D1, "greenhouse", "acme", 3)]
    events = [event(D1, "greenhouse", "acme", "added", f"j{i}") for i in range(3)]
    rows, _ = ex.summarise(counts, events)
    free = ex.publishable(rows, set())

    assert free, "the fixture must produce a row, or this test asserts nothing"
    for row in free:
        assert tuple(row) == ex.FREE_FIELDS, row


def test_free_output_bytes_contain_no_job_level_value():
    """Belt and braces on the serialised bytes, not just the dicts: the ruling forbids
    free text, URLs and ids, and a projection bug would still be caught here."""
    counts = [count(D1, "greenhouse", "acme", 1)]
    events = [event(D1, "greenhouse", "acme", "added")]
    rows, _ = ex.summarise(counts, events)
    free = ex.publishable(rows, set())

    for blob in (ex.to_csv(free, ex.FREE_FIELDS), ex.to_jsonl(free, ex.FREE_FIELDS)):
        text = blob.decode()
        leaks = ("Executive Assistant", "Privet Drive", "Engineering", "https://", "j1", "99")
        for leaked in leaks:
            assert leaked not in text, f"{leaked!r} leaked into the free tier: {text}"


def test_no_tier_ever_emits_a_forbidden_field():
    """Ad body, salary, raw payload, contact fields. None is in `EVENT_KEYS`, so this is a
    regression guard on someone widening the projection later."""
    assert ex.FORBIDDEN_FIELDS.isdisjoint(EVENT_KEYS)
    assert ex.FORBIDDEN_FIELDS.isdisjoint(ex.SUMMARY_FIELDS)
    assert ex.FORBIDDEN_FIELDS.isdisjoint(ex.FREE_FIELDS)

    poisoned = event(D1, "greenhouse", "acme", "added")
    poisoned |= {"description": "<p>the ad body</p>", "salary": "$200k", "recruiter": "Jane Roe"}
    kept = ex.trustworthy_events([poisoned], [count(D1, "greenhouse", "acme", 1)], set())
    assert tuple(kept[0]) == EVENT_KEYS
    assert "the ad body" not in ex.to_jsonl(kept, EVENT_KEYS).decode()


def test_personio_and_the_blocklist_never_reach_the_free_tier():
    counts = [
        count(D1, "personio", "acme-gmbh", 5),
        count(D1, "greenhouse", "blocked-co", 5),
        count(D1, "greenhouse", "kept", 5),
    ]
    rows, _ = ex.summarise(counts, [])
    free = ex.publishable(rows, {("greenhouse", "blocked-co")})
    assert [r["company"] for r in free] == ["kept"]

    # Personio stays in the paid feed (a contracted delivery is not a public directory);
    # an employer's own exclusion request is honoured at both tiers.
    paid = ex.unblocked(rows, {("greenhouse", "blocked-co")})
    assert sorted(r["company"] for r in paid) == ["acme-gmbh", "kept"]


# -------------------------------------------------------- §7.4 fetch-failure gates


def test_empty_board_wipe_is_suppressed_not_sold_as_a_closure():
    """`open == 0 and removed > 0` is only reachable through the EMPTY_SUSPECT chain."""
    counts = [count(D2, "lever", "wiped", 0), count(D2, "lever", "healthy", 4)]
    events = [
        *(event(D2, "lever", "wiped", "removed", f"j{i}") for i in range(5)),
        event(D2, "lever", "healthy", "removed", "h1"),
    ]
    rows, suppressed = ex.summarise(counts, events)

    assert suppressed == {(D2, "lever", "wiped")}
    assert [r["company"] for r in rows] == ["healthy"]
    assert rows[0]["removed"] == 1

    kept = ex.trustworthy_events(events, counts, suppressed)
    assert {e["company"] for e in kept} == {"healthy"}, "the archive must drop them too"


def test_a_day_we_failed_to_collect_is_absent_not_zero():
    """A failed fetch, a `stale` company and a degraded provider all write no counts row.
    Emitting a 0 there would report a full board as closed."""
    counts = [count(D1, "ashby", "acme", 10), count(D3, "ashby", "acme", 10)]
    events = [event(D2, "ashby", "acme", "removed", f"j{i}") for i in range(10)]

    rows, suppressed = ex.summarise(counts, events)
    assert [r["d"] for r in rows] == [D1, D3]
    assert not suppressed
    assert all(r["removed"] == 0 for r in rows), "unmeasured events must not be attributed"
    assert ex.trustworthy_events(events, counts, suppressed) == []


def test_open_zero_with_no_removals_is_a_real_measurement():
    """A board that was already empty is measured, not suppressed — it just has no events."""
    rows, suppressed = ex.summarise([count(D1, "lever", "quiet", 0)], [])
    assert not suppressed
    assert rows == [
        {
            "d": D1,
            "provider": "lever",
            "company": "quiet",
            "open": 0,
            "added": 0,
            "removed": 0,
            "net": 0,
        }
    ]


# ----------------------------------------------------------------- shape and maths


def test_summary_maths_and_that_changed_events_are_neither():
    counts = [count(D1, "ashby", "acme", 7)]
    events = [
        *(event(D1, "ashby", "acme", "added", f"a{i}") for i in range(3)),
        *(event(D1, "ashby", "acme", "removed", f"r{i}") for i in range(2)),
        event(D1, "ashby", "acme", "changed", "c1", changed=["t"]),
    ]
    rows, _ = ex.summarise(counts, events)
    assert rows == [
        {
            "d": D1,
            "provider": "ashby",
            "company": "acme",
            "open": 7,
            "added": 3,
            "removed": 2,
            "net": 1,
        }
    ]


def test_sample_window_is_the_three_most_recent_days():
    rows, _ = ex.summarise([count(d, "lever", "acme", 1) for d in (D1, D2, D3, D4)], [])
    assert sorted({r["d"] for r in ex.recent_days(rows)}) == [D2, D3, D4]


def test_day_keys_selects_by_kind_and_window():
    keys = [
        "counts.2026-08-26.0",
        "counts.2026-08-28.3",
        "events.2026-08-27.1",
        "state.00",
        "watchlist",
        "meta",
    ]
    assert ex.day_keys(keys, "counts", None, None) == ["counts.2026-08-26.0", "counts.2026-08-28.3"]
    assert ex.day_keys(keys, "counts", D2, None) == ["counts.2026-08-28.3"]
    assert ex.day_keys(keys, "events", None, D2) == ["events.2026-08-27.1"]
    assert ex.day_keys(keys, "counts", D4, None) == []


def test_export_writes_both_tiers_to_disk(tmp_path):
    counts = [count(D1, "greenhouse", "acme", 2), count(D1, "personio", "gmbh", 2)]
    events = [event(D1, "greenhouse", "acme", "added", f"j{i}") for i in range(2)]
    rows, suppressed = ex.summarise(counts, events)
    archive = ex.trustworthy_events(events, counts, suppressed)

    ex.export(rows, archive, tmp_path / "free", sample=True, blocked=set())
    sample = (tmp_path / "free" / "sample" / "closures-72h.csv").read_text()
    assert sample.splitlines()[0] == ",".join(ex.FREE_FIELDS)
    assert "personio" not in sample
    assert (tmp_path / "free" / "sample" / "closures-72h.jsonl").exists()

    ex.export(rows, archive, tmp_path / "paid", sample=False, blocked=set())
    summary = (tmp_path / "paid" / "summary" / "closures-daily.csv").read_text()
    assert summary.splitlines()[0] == ",".join(ex.SUMMARY_FIELDS)
    assert "personio" in summary, "the paid tier keeps every provider"

    part = tmp_path / "paid" / "archive" / D1 / "events.jsonl.gz"
    lines = gzip.decompress(part.read_bytes()).decode().splitlines()
    assert len(lines) == 2
    assert tuple(json.loads(lines[0])) == EVENT_KEYS
    assert gzip.decompress((tmp_path / "paid" / "archive" / D1 / "events.csv.gz").read_bytes())


def test_a_windowed_run_cannot_overwrite_the_full_depth_summary(tmp_path):
    """`closures-daily.csv` is what the "full, from <first day>" tier is delivered from. A
    `--since`/`--until` run holds only part of the store, so it must name its own file."""
    counts = [count(D1, "greenhouse", "acme", 2), count(D3, "greenhouse", "acme", 2)]
    rows, _ = ex.summarise(counts, [])

    ex.export(rows, [], tmp_path, sample=False, blocked=set())
    full = tmp_path / "summary" / "closures-daily.csv"
    assert len(full.read_text().splitlines()) == 3  # header + both days

    narrow, _ = ex.summarise([count(D3, "greenhouse", "acme", 2)], [])
    ex.export(narrow, [], tmp_path, sample=False, blocked=set(), windowed=True)
    assert len(full.read_text().splitlines()) == 3, "the full-depth file was overwritten"
    assert (tmp_path / "summary" / f"closures-daily.{D3}_{D3}.csv").exists()


def test_csv_renders_none_as_empty_and_changed_as_a_list():
    blob = ex.to_csv(
        [{**event(D1, "lever", "acme", "changed"), "days_open": None, "changed": ["t", "loc"]}],
        EVENT_KEYS,
    ).decode()
    row = blob.splitlines()[1]
    assert "t|loc" in row
    assert row.endswith(",,t|loc,"), row  # days_open and verified empty, not "None"
