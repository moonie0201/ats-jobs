"""§4.5.6 identity, dedupe and change detection.

Shuffle-invariance is the highest-value test in this file: a provider reordering its
location array must not flip ``changeHash``, or the history dataset the moat depends on
fills with phantom ``loc`` changes.
"""

from __future__ import annotations

import pytest

from core.models import Ref
from core.normalize.identity import (
    apply_identity,
    canon,
    canon_company,
    canon_locations,
    change_hash,
    content_key,
    dedupe_key,
    fmt_money,
    make_id,
    normalize_title,
)
from core.normalize.record import build_job_record

OPTIONS = {"scrapedAt": "2026-08-26T03:00:12Z"}


def record(**extracted):
    base = {"sourceId": "4019283", "title": "Senior Backend Engineer", "company": "Anthropic"}
    return build_job_record(Ref("greenhouse", "anthropic"), base | extracted, OPTIONS)


# --- canonicalization -----------------------------------------------------------------


def test_canon():
    assert canon("  Senior   Engineer  ") == "senior engineer"
    assert canon(None) == ""
    assert canon("") == ""
    assert canon("Ünïcode") == "ünïcode"


def test_canon_locations_is_sorted_and_deduped():
    assert canon_locations(["Berlin", "austin", "Berlin", None, ""]) == "austin|berlin"
    assert canon_locations([]) == ""
    assert canon_locations(None) == ""


def test_fmt_money_hashes_int_and_float_alike():
    assert fmt_money(180000) == fmt_money(180000.0)
    assert fmt_money(None) == ""
    assert fmt_money(0) == "0.00"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Anthropic, Inc.", "anthropic"),
        ("Acme GmbH", "acme"),
        ("Foo Ltd.", "foo"),
        ("Bar B.V.", "bar"),
        ("Widgets Pty Ltd", "widgets"),
        ("Stripe", "stripe"),
        ("Cisco", "cisco"),  # a suffix is only stripped as a whole word
        (None, ""),
    ],
)
def test_canon_company(name, expected):
    assert canon_company(name) == expected


@pytest.mark.parametrize(
    ("title", "city", "expected"),
    [
        ("Senior Backend Engineer, Inference", None, "senior backend engineer, inference"),
        ("Warehouse Associate III (12345)", None, "warehouse associate iii"),
        ("Engineer #48210", None, "engineer"),
        ("Engineer [102934]", None, "engineer"),
        ("Engineer - Berlin", "Berlin", "engineer"),
        ("Engineer (Berlin)", "Berlin", "engineer"),
        ("Engineer - Berlin", "Munich", "engineer - berlin"),  # only the parsed city goes
        ("Engineer (Remote)", None, "engineer"),
        ("Engineer - Hybrid", None, "engineer"),
        ("Engineer - Berlin (Remote) #12345", "Berlin", "engineer"),
        ("Engineer  --  Backend", None, "engineer - backend"),
        ("Engineer", None, "engineer"),
        (None, None, None),
        ("", None, None),
    ],
)
def test_normalize_title(title, city, expected):
    assert normalize_title(title, city) == expected


# --- the three keys -------------------------------------------------------------------


def test_id_is_lower_case_and_stable_across_runs():
    assert make_id("Greenhouse", "Anthropic", "4019283") == "greenhouse:anthropic:4019283"
    assert record().id == record().id == "greenhouse:anthropic:4019283"


def test_content_key_separates_distinct_requisitions():
    # Twelve identical warehouse requisitions in one city are twelve real openings.
    a = record(title="Warehouse Associate III", locationRaw="Dallas, TX", requisitionId="R-1")
    b = record(title="Warehouse Associate III", locationRaw="Dallas, TX", requisitionId="R-2")
    same = record(title="Warehouse Associate III", locationRaw="Dallas, TX", requisitionId="R-1")
    assert a.contentKey != b.contentKey
    assert a.contentKey == same.contentKey


def test_content_key_separates_remote_roles_with_a_null_city():
    # §4.5.1 step 6 nulls the city for region-only text, so the raw string is the only
    # thing keeping two continents apart.
    emea = record(title="Support Engineer", locationRaw="Remote (EMEA)")
    apac = record(title="Support Engineer", locationRaw="Remote (APAC)")
    assert emea.city is apac.city is None
    assert emea.contentKey != apac.contentKey


def test_content_key_ignores_a_requisition_id_repeated_in_the_title():
    assert content_key("engineer", "Acme", "Berlin", "1") != content_key(
        "engineer", "Acme", "Berlin", "2"
    )


def test_change_hash_ignores_the_description():
    plain = record(descriptionHtml="<p>We are hiring!</p>")
    edited = record(descriptionHtml="<p>We are hiring — apply today!</p>")
    assert plain.changeHash == edited.changeHash


def test_change_hash_reacts_to_the_title():
    assert record(title="Engineer").changeHash != record(title="Senior Engineer").changeHash


def test_none_department_and_the_string_none_hash_differently():
    assert record(department=None).changeHash != record(department="None").changeHash


def test_change_hash_inputs_are_canonicalized():
    assert change_hash("Engineer", ["Berlin"], "Eng", True, "full_time", 180000, 240000.0) == (
        change_hash("  engineer ", ["berlin"], "eng", True, "full_time", 180000.0, 240000)
    )


def test_shuffle_invariance():
    """The same job twice with its location arrays shuffled: identical everything."""
    shuffled = [
        ["Berlin, Germany", "Austin, TX", "Toronto, ON, Canada"],
        ["Toronto, ON, Canada", "Berlin, Germany", "Austin, TX"],
        ["Austin, TX", "Toronto, ON, Canada", "Berlin, Germany"],
    ]
    built = [
        record(locationRaw="Berlin, Germany", locations=order, department="Engineering")
        for order in shuffled
    ]
    assert len({r.changeHash for r in built}) == 1
    assert len({r.contentKey for r in built}) == 1
    assert len({tuple(loc.sort_key for loc in r.locations) for r in built}) == 1


def test_dedupe_key_modes():
    row = record()
    assert dedupe_key(row) == row.id
    assert dedupe_key(row, "content") == row.contentKey


def test_apply_identity_fills_all_four_fields():
    row = apply_identity(record(title="Engineer - Berlin", locationRaw="Berlin, Germany"))
    assert row.id and row.contentKey and row.changeHash
    assert row.titleNormalized == "engineer"
    assert len(row.contentKey) == 16 and len(row.changeHash) == 8
