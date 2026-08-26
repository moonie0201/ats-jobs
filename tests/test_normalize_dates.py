"""§4.5.5 dates: ISO 8601 UTC out, ``YYYY-MM-DD`` into the history state, null when unsure."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from core.normalize.dates import pick_date, to_date, to_iso_utc, utc_now_iso

CASES = [
    ("2026-08-19", "2026-08-19T00:00:00Z"),  # naive date -> midnight UTC
    ("2026-08-19T00:00:00Z", "2026-08-19T00:00:00Z"),
    ("2024-11-13T14:10:41+00:00", "2024-11-13T14:10:41Z"),  # Personio createdAt, verified
    ("2026-08-22T11:04:00", "2026-08-22T11:04:00Z"),  # naive datetime is UTC
    ("2026-08-19T12:00:00+02:00", "2026-08-19T10:00:00Z"),  # offsets are converted
    ("2026-08-19T00:00:00.123456Z", "2026-08-19T00:00:00Z"),  # microseconds dropped
    (1755561600000, "2025-08-19T00:00:00Z"),  # Lever createdAt, epoch ms
    ("1755561600000", "2025-08-19T00:00:00Z"),
    (1755561600, "2025-08-19T00:00:00Z"),  # epoch seconds
    (date(2026, 8, 19), "2026-08-19T00:00:00Z"),
    (datetime(2026, 8, 19, 5, 30, tzinfo=UTC), "2026-08-19T05:30:00Z"),
    ("Posted 30+ Days Ago", None),  # Workday's relative text is not a date
    ("yesterday", None),
    ("", None),
    ("   ", None),
    (None, None),
    (0, None),
    (True, None),
    ([], None),
]


@pytest.mark.parametrize(("value", "expected"), CASES)
def test_to_iso_utc(value, expected):
    assert to_iso_utc(value) == expected


def test_to_date_is_the_ten_character_storage_form():
    assert to_date("2026-08-19T23:59:59Z") == "2026-08-19"
    assert to_date(1755561600000) == "2025-08-19"
    assert to_date("Posted 30+ Days Ago") is None


def test_pick_date_names_the_field_the_date_came_from():
    assert pick_date(("first_published", None), ("updated_at", "2026-08-22")) == (
        "2026-08-22T00:00:00Z",
        "updated_at",
    )
    assert pick_date(("first_published", "2026-08-19"), ("updated_at", "2026-08-22")) == (
        "2026-08-19T00:00:00Z",
        "first_published",
    )
    assert pick_date(("first_published", None), ("updated_at", "")) == (None, None)
    assert pick_date() == (None, None)


def test_utc_now_iso_shape():
    now = utc_now_iso()
    assert now.endswith("Z") and len(now) == 20
    assert datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ")
