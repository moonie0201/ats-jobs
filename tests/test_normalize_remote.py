"""§4.5.2 remote detection: the six-rank ladder and what must never fire."""

from __future__ import annotations

import pytest

from core.normalize.location import parse_location
from core.normalize.remote import detect_remote

# (job, location, title, description_head) -> (remote, workplaceType, remoteSource)
LADDER = [
    # Rank 1 — ATS structured flags beat everything below them.
    (
        {"isRemote": True, "workplaceType": "Remote"},
        "Berlin",
        "Engineer",
        None,
        (True, "remote", "ats"),
    ),
    (
        {"workplaceType": "Hybrid"},
        "Remote - US",
        "Remote Engineer",
        "fully remote",
        (False, "hybrid", "ats"),
    ),
    ({"workplaceType": "OnSite"}, "Remote - US", None, None, (False, "onsite", "ats")),
    ({"workplaceType": "on-site"}, "Berlin", None, None, (False, "onsite", "ats")),  # Lever
    ({"remote": True}, "Berlin", None, None, (True, "remote", "ats")),  # Recruitee
    ({"hybrid": True}, "Berlin", None, None, (False, "hybrid", "ats")),
    ({"on_site": True}, "Remote", None, None, (False, "onsite", "ats")),
    ({"isRemote": False}, "Berlin", "Engineer", None, (False, None, "ats")),
    # Lever "unspecified" is not an answer — it falls through to the text ranks.
    ({"workplaceType": "unspecified"}, "Remote - US", None, None, (True, "remote", "location")),
    ({"workplaceType": "unspecified"}, "Berlin", "Engineer", None, (None, None, None)),
    # Rank 2 — the marker §4.5.1 step 3 stripped, or region-only text.
    ({}, "Remote - US", None, None, (True, "remote", "location")),
    ({}, "Remote (EMEA)", None, None, (True, "remote", "location")),
    ({}, "Anywhere", None, None, (True, "remote", "location")),
    ({}, "Worldwide", None, None, (True, "remote", "location")),
    ({}, "Hybrid - Dublin, Ireland", None, None, (False, "hybrid", "location")),
    ({}, "Berlin (On-Site)", None, None, (False, "onsite", "location")),
    ({}, "EMEA", None, None, (None, None, None)),  # a macro-region is not a remote flag
    # Rank 3 — the raw location string.
    ({}, "Berlin, work from home possible", None, None, (True, "remote", "location")),
    ({}, "US (Telecommute)", None, None, (True, "remote", "location")),
    ({}, "WFH friendly", None, None, (True, "remote", "location")),
    # Rank 4 — the title, only after the location said nothing.
    ({}, "Berlin", "Senior Engineer (Remote)", None, (True, "remote", "title")),
    ({}, "Berlin", "Hybrid Account Executive", None, (False, "hybrid", "title")),
    ({}, None, "Remote Support Lead", None, (True, "remote", "title")),
    # Rank 5 — two narrow patterns, first 1,500 characters only.
    ({}, "Berlin", "Engineer", "This is a fully remote role.", (True, "remote", "description")),
    ({}, "Berlin", "Engineer", "100% remote team", (True, "remote", "description")),
    ({}, "Berlin", "Engineer", "Location: Remote (Germany)", (True, "remote", "description")),
    ({}, "Berlin", "Engineer", "Work type: remote", (True, "remote", "description")),
    # Rank 6 — nothing matched.
    ({}, "Berlin, Germany", "Engineer", "We are a remote-first culture.", (None, None, None)),
    ({}, None, None, None, (None, None, None)),
]


@pytest.mark.parametrize(("job", "location", "title", "head", "expected"), LADDER)
def test_ladder(job, location, title, head, expected):
    assert detect_remote(job, location, title, head) == expected


def test_description_boilerplate_beyond_the_cap_is_ignored():
    head = "x" * 3000 + " this is a fully remote role"
    assert detect_remote({}, "Berlin", "Engineer", head) == (None, None, None)


def test_description_is_only_read_when_the_caller_offers_it():
    # ``includeDescription: false`` means record.py passes None — rank 5 cannot fire.
    assert detect_remote({}, "Berlin", "Engineer", None) == (None, None, None)


def test_hybrid_never_yields_remote_true():
    for text in ("Hybrid - Berlin", "Berlin (Hybrid)", "Hybrid Remote - Berlin"):
        remote, workplace, _ = detect_remote({}, text, None, None)
        assert (remote, workplace) == (False, "hybrid"), text


def test_accepts_a_location_object():
    location = parse_location("Remote - US")
    assert detect_remote({}, location, None, None) == (True, "remote", "location")


def test_missing_job_dict_is_not_an_error():
    assert detect_remote(None, "Remote", None, None) == (True, "remote", "location")
