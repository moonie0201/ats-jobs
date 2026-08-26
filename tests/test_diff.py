"""§7.4 diff — pure, and the three v1 corrections are pinned by name here.

History cannot be backfilled: a day lost to a `KeyError` is lost forever, which is why
the malformed-state case gets its own test.
"""

from __future__ import annotations

from core.diff import EVENT_KEYS, canon, diff, jhash

TODAY = "2026-08-26"
PROVIDER = "greenhouse"
COMPANY = "anthropic"


def fetched(job_id="1", **kwargs):
    base = {
        "id": job_id,
        "t": "Backend Engineer",
        "loc": "San Francisco, CA",
        "dept": "Engineering",
        "remote": False,
        "url": "https://job-boards.greenhouse.io/anthropic/jobs/1",
        "posted": "2026-08-01",
    }
    return {**base, **kwargs}


def stored(job, *, today=TODAY, **overrides):
    state, _ = diff({}, [job], today, PROVIDER, COMPANY)
    state[str(job["id"])].update(overrides)
    return state


def test_first_run_adds_everything():
    state, events = diff({}, [fetched()], TODAY, PROVIDER, COMPANY)
    assert [e["ev"] for e in events] == ["added"]
    assert state["1"]["first_seen"] == TODAY
    assert state["1"]["last_seen"] == TODAY
    assert state["1"]["posted"] == "2026-08-01"
    assert state["1"]["posted_src"] == "api"


def test_unchanged_job_emits_nothing():
    prev = stored(fetched())
    state, events = diff(prev, [fetched()], "2026-08-27", PROVIDER, COMPANY)
    assert events == []
    assert state["1"]["first_seen"] == TODAY
    assert state["1"]["last_seen"] == "2026-08-27"


def test_changed_job_lists_only_the_fields_that_moved():
    prev = stored(fetched())
    state, events = diff(
        prev, [fetched(t="Staff Backend Engineer")], "2026-08-27", PROVIDER, COMPANY
    )
    assert len(events) == 1
    assert events[0]["ev"] == "changed"
    assert events[0]["changed"] == ["t"]
    assert events[0]["t"] == "Staff Backend Engineer"
    assert state["1"]["h"] == jhash(fetched(t="Staff Backend Engineer"))


def test_removed_job_carries_days_open():
    prev = stored(fetched())
    state, events = diff(prev, [], "2026-08-27", PROVIDER, COMPANY)
    assert state == {}
    assert events[0]["ev"] == "removed"
    assert events[0]["days_open"] == 26  # 2026-08-01 -> 2026-08-27


def test_events_carry_the_documented_keys_and_nothing_else():
    """v1 splatted state records, leaking `h`, `first_seen` and a stale `last_seen` into
    every row and inflating the §7.7 event budget (correction 3)."""
    prev = stored(fetched())
    _, events = diff(prev, [fetched(t="new")], "2026-08-27", PROVIDER, COMPANY)
    _, removals = diff(prev, [], "2026-08-27", PROVIDER, COMPANY)
    for event in [*events, *removals]:
        assert tuple(event) == EVENT_KEYS


def test_state_without_posted_does_not_kill_the_bucket():
    """Correction 1: one bad record used to raise KeyError and lose ~80 companies' day."""
    prev = {"1": {"t": "Backend Engineer", "h": "deadbeef"}}
    state, events = diff(prev, [], TODAY, PROVIDER, COMPANY)
    assert state == {}
    assert events[0]["days_open"] is None


def test_state_without_h_is_treated_as_changed_not_fatal():
    prev = {"1": {"t": "Backend Engineer", "posted": "2026-08-01"}}
    _, events = diff(prev, [fetched()], TODAY, PROVIDER, COMPANY)
    assert events[0]["ev"] == "changed"


def test_days_open_is_never_negative():
    """Correction 2: providers backdate; a future `posted` fed a negative into the
    flagship time-to-fill metric."""
    prev = stored(fetched(posted="2027-01-01"))
    _, events = diff(prev, [], TODAY, PROVIDER, COMPANY)
    assert events[0]["days_open"] == 0


def test_unparseable_posted_gives_a_null_not_a_crash():
    prev = {"1": {"posted": "not-a-date"}}
    _, events = diff(prev, [], TODAY, PROVIDER, COMPANY)
    assert events[0]["days_open"] is None


def test_posted_is_stored_as_ten_characters():
    """§4.5.5: `date.fromisoformat` would raise on the timestamp form."""
    state, _ = diff({}, [fetched(posted="2026-08-01T14:10:41+00:00")], TODAY, PROVIDER, COMPANY)
    assert state["1"]["posted"] == "2026-08-01"


def test_missing_posted_falls_back_to_today_and_records_the_source():
    state, _ = diff({}, [fetched(posted=None)], TODAY, PROVIDER, COMPANY)
    assert state["1"]["posted"] == TODAY
    assert state["1"]["posted_src"] == "snapshot"


def test_first_posted_wins_over_later_reports():
    prev = stored(fetched())
    state, _ = diff(prev, [fetched(posted="2026-08-20")], "2026-08-27", PROVIDER, COMPANY)
    assert state["1"]["posted"] == "2026-08-01"


def test_reappearing_id_is_a_fresh_add():
    """A re-post is a real signal (§7.4 safety table)."""
    _, events = diff({}, [fetched()], "2026-09-01", PROVIDER, COMPANY)
    assert events[0]["ev"] == "added"


def test_hash_distinguishes_a_null_department_from_the_string_none():
    """Correction 3: v1 hashed `str(x)`, so `None` collided with the literal "None"."""
    assert jhash(fetched(dept=None)) != jhash(fetched(dept="None"))


def test_hash_ignores_provider_reordering_of_locations():
    """An unsorted array must not emit a phantom `loc` change every single day."""
    assert jhash(fetched(loc=["Berlin", "Paris"])) == jhash(fetched(loc=["Paris", "Berlin"]))


def test_hash_ignores_whitespace_and_case():
    assert jhash(fetched(t="Backend  Engineer")) == jhash(fetched(t="backend engineer"))


def test_hash_ignores_fields_outside_the_change_set():
    """`changeHash` deliberately excludes cosmetic edits (§4.5.6)."""
    assert jhash(fetched(url="https://elsewhere")) == jhash(fetched())


def test_canon_of_a_bool_is_lowercase():
    assert canon(False) == "false"
    assert canon(None) == ""


def test_a_custom_hasher_can_be_injected():
    calls: list[dict] = []

    def hasher(job):
        calls.append(job)
        return "constant"

    prev = {"1": {"h": "constant", "posted": "2026-08-01"}}
    _, events = diff(prev, [fetched()], TODAY, PROVIDER, COMPANY, hasher=hasher)
    assert events == []
    assert len(calls) == 1


def test_int_ids_are_normalised_to_strings():
    state, events = diff({}, [fetched(job_id=4567)], TODAY, PROVIDER, COMPANY)
    assert set(state) == {"4567"}
    assert events[0]["job_id"] == "4567"
