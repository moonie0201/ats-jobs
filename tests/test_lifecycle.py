"""`core.lifecycle` — the enrichment read path (SPEC v2 §7 history, Actor-to-Actor input).

The thing these tests defend is not the happy path. It is that every way of not knowing
stays visibly different from every other way, because a lifecycle product that answers
"we do not know" as if it were "this job never closed" is worse than one that answers
nothing at all.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.lifecycle import (  # noqa: E402
    COVERAGE_BOARD_UNTRACKED,
    COVERAGE_JOB_UNKNOWN,
    COVERAGE_TRACKED,
    COVERAGE_UNRESOLVED,
    COVERAGE_UNSUPPORTED,
    Enricher,
    IndexError_,
    first_url,
    is_chargeable,
    load_index,
    parse_posting_url,
)

TODAY = date(2026, 9, 8)
UUID = "5d059b32-d73e-4890-8d63-9d565d3c4e95"


def boards_with(provider: str, company: str, *, tracked_since: str, jobs: dict) -> dict:
    """One board in the published index shape: rows are [job_id, posted, first_seen]."""
    return load_index(
        {
            "schema": 1,
            "boards": [
                {
                    "p": provider,
                    "c": company,
                    "t": tracked_since,
                    "j": [
                        [jid, job.get("posted"), job.get("first_seen")] for jid, job in jobs.items()
                    ],
                }
            ],
        }
    )


# ---------------------------------------------------------------- identity


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://jobs.lever.co/BestEgg/{UUID}", ("lever", "BestEgg", UUID)),
        (f"https://jobs.eu.lever.co/seb/{UUID}", ("lever", "seb", UUID)),
        (f"https://jobs.ashbyhq.com/ramp/{UUID}", ("ashby", "ramp", UUID)),
        (f"https://ats.rippling.com/acme/jobs/{UUID}", ("rippling", "acme", UUID)),
    ],
)
def test_supported_urls_resolve_to_the_boards_own_key(url, expected):
    """Verified 2026-09-05 against all three live APIs: the UUID in the path is the same
    string the board API returns as its job key. That is why only these three are here."""
    ref = parse_posting_url(url)
    assert (ref.provider, ref.company, ref.job_id) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://stripe.com/jobs/search?gh_jid=8172508",  # greenhouse via employer domain
        "https://job-boards.greenhouse.io/stripe/jobs/8172508",
        "https://carano.recruitee.com/o/initiativbewerbung",  # slug, no id at all
        "https://ottonova.jobs.personio.de/job/1",
    ],
)
def test_providers_we_cannot_identify_are_named_not_guessed(url):
    """Greenhouse hides the id in a `gh_jid` query param on the employer's own domain and
    Recruitee URLs carry a mutable title slug with no id. Guessing an identity here would
    attach one job's history to another."""
    from core.lifecycle import unsupported_provider

    assert parse_posting_url(url) is None
    assert unsupported_provider(url) is not None


@pytest.mark.parametrize(
    "url",
    [
        f"https://evil.example/redirect?u=https://jobs.lever.co/acme/{UUID}",
        f"https://jobs.lever.co.attacker.test/acme/{UUID}",
        "https://jobs.ashbyhq.com/ramp/not-a-uuid",
        "",
        None,
        12345,
    ],
)
def test_near_misses_resolve_to_nothing(url):
    """The pattern is anchored on the provider's own host. A URL that merely contains one
    is not one — the open-redirect shape is exactly how a wrong company gets attached."""
    assert parse_posting_url(url) is None


# ---------------------------------------------------------------- coverage


def test_a_tracked_live_job_answers_from_the_index():
    boards = boards_with(
        "ashby",
        "ramp",
        tracked_since="2026-08-26",
        jobs={UUID: {"first_seen": "2026-08-28", "last_seen": "2026-09-08", "posted": None}},
    )
    row = Enricher(boards=boards, today=TODAY).enrich(f"https://jobs.ashbyhq.com/ramp/{UUID}")
    assert row["coverage"] == COVERAGE_TRACKED
    assert row["stillListed"] is True
    assert row["firstSeen"] == "2026-08-28"
    assert row["firstSeenCensored"] is False
    assert (row["ageDays"], row["ageBasis"]) == (11, "observed")


def test_the_boards_own_posted_date_beats_our_first_sighting():
    """The value of the product. Measured on a live ramp posting 2026-09-08: the board had
    been watched for 4 days and the answer was 154 days, because Ashby publishes
    `publishedAt`. Reporting 4 would have been true about us and useless about the job."""
    boards = boards_with(
        "ashby",
        "ramp",
        tracked_since="2026-09-04",
        jobs={
            UUID: {"first_seen": "2026-09-04", "posted": "2026-04-07", "posted_src": "publishedAt"}
        },
    )
    row = Enricher(boards=boards, today=TODAY).enrich(f"https://jobs.ashbyhq.com/ramp/{UUID}")
    assert (row["ageDays"], row["ageBasis"]) == (154, "board")
    assert row["historyWindowDays"] == 4
    assert row["firstSeenCensored"] is True


def test_a_one_day_window_cannot_read_as_never_closed():
    """A board added yesterday can only say "still up after one day". Without
    `historyWindowDays` and the censoring flag, `stillListed: true` reads as a claim about
    the job's whole life instead of about our one day of looking."""
    boards = boards_with(
        "lever",
        "acme",
        tracked_since="2026-09-07",
        jobs={UUID: {"first_seen": "2026-09-07", "posted": None}},
    )
    row = Enricher(boards=boards, today=TODAY).enrich(f"https://jobs.lever.co/acme/{UUID}")
    assert row["historyWindowDays"] == 1
    assert row["firstSeenCensored"] is True
    assert row["ageBasis"] == "observed_floor", "a floor must not be presented as an age"


def test_the_four_ways_of_not_knowing_stay_distinct():
    boards = boards_with(
        "lever",
        "acme",
        tracked_since="2026-08-26",
        jobs={UUID: {"first_seen": "2026-08-26", "posted": None}},
    )
    enricher = Enricher(boards=boards, today=TODAY)
    other = "11111111-2222-3333-4444-555555555555"
    cases = {
        f"https://jobs.lever.co/acme/{other}": COVERAGE_JOB_UNKNOWN,
        f"https://jobs.lever.co/never-watched/{UUID}": COVERAGE_BOARD_UNTRACKED,
        "https://stripe.com/jobs/search?gh_jid=1": COVERAGE_UNSUPPORTED,
        "https://example.com/careers/123": COVERAGE_UNRESOLVED,
    }
    for url, expected in cases.items():
        row = enricher.enrich(url)
        assert row["coverage"] == expected, url
        assert row["stillListed"] is None, f"{url} must not imply a listing state"
        assert row["ageDays"] is None


def test_a_missing_id_on_a_watched_board_is_not_a_removal():
    """The tempting inference and the wrong one. We may have started watching after it
    closed, or the upstream row may simply be stale; `stillListed: false` would assert a
    removal we never observed."""
    boards = boards_with(
        "lever", "acme", tracked_since="2026-08-26", jobs={UUID: {"first_seen": "2026-08-26"}}
    )
    row = Enricher(boards=boards, today=TODAY).enrich(
        "https://jobs.lever.co/acme/99999999-9999-9999-9999-999999999999"
    )
    assert row["coverage"] == COVERAGE_JOB_UNKNOWN
    assert row["stillListed"] is None


def test_the_index_answers_without_further_io():
    """The whole reason this reads a published file rather than our key-value store: an
    Actor runs under the caller's account and cannot reach our storage. Once the index is
    loaded, enrichment is pure lookup — no per-row request to anywhere."""
    boards = boards_with(
        "lever", "acme", tracked_since="2026-08-26", jobs={UUID: {"first_seen": "2026-08-26"}}
    )
    enricher = Enricher(boards=boards, today=TODAY)
    rows = [enricher.enrich(f"https://jobs.lever.co/acme/{UUID}") for _ in range(5)]
    assert all(row["coverage"] == COVERAGE_TRACKED for row in rows)


def test_the_index_round_trips_the_published_shape():
    """`load_index` flips the file's board-nested form into the lookup enrich() makes. If
    the publisher's shape and this reader drift apart, every row silently becomes
    board_untracked — the failure would look like 'we watch nothing'."""
    boards = load_index(
        {
            "schema": 1,
            "boards": [
                {
                    "p": "ashby",
                    "c": "ramp",
                    "t": "2026-09-04",
                    "j": [[UUID, "2026-04-07", "2026-09-04"]],
                }
            ],
        }
    )
    row = Enricher(boards=boards, today=TODAY).enrich(f"https://jobs.ashbyhq.com/ramp/{UUID}")
    assert row["coverage"] == COVERAGE_TRACKED
    assert (row["postedAt"], row["ageDays"], row["ageBasis"]) == ("2026-04-07", 154, "board")


# ---------------------------------------------------------------- billing


def test_only_a_delivered_lifecycle_fact_is_chargeable():
    assert is_chargeable({"coverage": COVERAGE_TRACKED, "ageDays": 154, "ageBasis": "board"})
    for row in (
        {"coverage": COVERAGE_TRACKED, "ageDays": None},
        {"coverage": COVERAGE_JOB_UNKNOWN, "ageDays": None},
        {"coverage": COVERAGE_BOARD_UNTRACKED, "ageDays": None},
        {"coverage": COVERAGE_UNSUPPORTED, "ageDays": None},
        {"coverage": COVERAGE_UNRESOLVED, "ageDays": None},
    ):
        assert not is_chargeable(row), row


# ---------------------------------------------------------------- input shape


def test_the_configured_field_wins_and_a_rename_does_not_break_the_run():
    row = {"jobUrl": f"https://jobs.lever.co/acme/{UUID}", "url": "https://example.com/x"}
    assert first_url(row, ("jobUrl", "url")).endswith(UUID)
    # Upstream renamed the column: fall back to any value that parses as a posting URL.
    assert first_url({"link": f"https://jobs.ashbyhq.com/ramp/{UUID}"}, ("jobUrl",)).endswith(UUID)
    assert first_url({"title": "Engineer"}, ("jobUrl",)) is None


# --- Defects a Codex review found on 2026-09-09, each reproduced before it was fixed. ---


def test_a_uuid_with_a_suffix_is_not_that_uuid():
    """`.../{uuid}garbage` resolved to `{uuid}` and inherited a real posting's history."""
    for suffix in ("garbage", "-", "0"):
        assert parse_posting_url(f"https://jobs.lever.co/acme/{UUID}{suffix}") is None
    for terminator in ("", "/", "/apply", "?src=x", "#top"):
        ref = parse_posting_url(f"https://jobs.lever.co/acme/{UUID}{terminator}")
        assert ref is not None and ref.job_id == UUID


def test_a_floor_on_the_age_is_not_billed_as_the_age():
    """`observed_floor` says "at least N days" — on a board watched since yesterday, N=1."""
    boards = boards_with(
        "lever",
        "acme",
        tracked_since="2026-09-07",
        jobs={UUID: {"posted": None, "first_seen": "2026-09-07"}},
    )
    row = Enricher(boards=boards, today=TODAY).enrich(f"https://jobs.lever.co/acme/{UUID}")
    assert row["ageBasis"] == "observed_floor"
    assert row["ageDays"] == 1
    assert row["firstSeenCensored"] is True
    assert not is_chargeable(row)


def test_a_date_in_the_future_is_not_an_age():
    """A scheduled posting or a feed typo produced `ageDays: -2`, charged like any other."""
    boards = boards_with(
        "lever",
        "acme",
        tracked_since="2026-08-26",
        jobs={UUID: {"posted": "2026-09-10", "first_seen": "2026-08-26"}},
    )
    row = Enricher(boards=boards, today=TODAY).enrich(f"https://jobs.lever.co/acme/{UUID}")
    assert row["ageDays"] is None
    assert not is_chargeable(row)


def test_an_index_this_build_cannot_read_is_refused():
    """A schema bump that swaps two date columns must not return confident ages."""
    with pytest.raises(IndexError_):
        load_index({"schema": 2, "boards": []})
    with pytest.raises(IndexError_):
        load_index({"boards": []})
    with pytest.raises(IndexError_):  # a row that is not [id, posted, first_seen]
        load_index({"schema": 1, "boards": [{"p": "lever", "c": "acme", "j": [[UUID]]}]})


def test_a_duplicated_board_is_refused_rather_than_half_dropped():
    """Two entries for one board silently kept the last; the other board's jobs vanished."""
    with pytest.raises(IndexError_):
        load_index(
            {
                "schema": 1,
                "boards": [
                    {
                        "p": "lever",
                        "c": "acme",
                        "t": "2026-01-01",
                        "j": [[UUID, "2026-01-01", "x"]],
                    },
                    {"p": "lever", "c": "acme", "t": "2026-09-07", "j": [["b", "2026-09-07", "y"]]},
                ],
            }
        )


def test_the_field_that_resolves_beats_the_field_that_is_merely_present():
    """`url` holding a careers page suppressed `jobUrl` holding the posting."""
    row = {
        "url": "https://acme.com/careers",
        "jobUrl": f"https://jobs.lever.co/acme/{UUID}",
    }
    assert first_url(row, ("url", "jobUrl")) == row["jobUrl"]
    # Nothing resolves: still report what the caller pointed at, so the row names its URL.
    assert first_url({"url": "https://acme.com/careers"}, ("url",)) == "https://acme.com/careers"
