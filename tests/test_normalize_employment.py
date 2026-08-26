"""§4.5.4 employment type: every provider vocabulary, and the provenance that bills."""

from __future__ import annotations

import pytest

from core.normalize.employment import detect_employment, employment_from_title

# (provider, provider value) -> normalized enum value
VOCABULARIES = [
    ("ashby", "FullTime", "full_time"),
    ("ashby", "PartTime", "part_time"),
    ("ashby", "Intern", "internship"),
    ("ashby", "Contract", "contract"),
    ("ashby", "Temporary", "temporary"),
    ("lever", "Full-time", "full_time"),
    ("lever", "Part-time", "part_time"),
    ("lever", "Contract", "contract"),
    ("lever", "Intern", "internship"),
    ("lever", "Internship", "internship"),
    ("lever", "Temporary", "temporary"),
    ("recruitee", "fulltime", "full_time"),
    ("recruitee", "parttime", "part_time"),
    ("recruitee", "contract", "contract"),
    ("recruitee", "internship", "internship"),
    ("recruitee", "freelance", "contract"),
    ("recruitee", "temporary", "temporary"),
    ("personio", "permanent", "full_time"),
    ("personio", "intern", "internship"),
    ("personio", "trainee", "internship"),
    ("personio", "working-student", "part_time"),
    ("personio", "freelance", "contract"),
    ("personio", "temporary", "temporary"),
    # Rippling sends the machine token in ``label`` and the human string in ``id`` —
    # the inverse of every other provider (V2 T-C2). Both forms must resolve.
    ("rippling", {"label": "SALARIED_FT", "id": "Salaried, full-time"}, "full_time"),
    ("rippling", {"label": "SALARIED_PT", "id": "Salaried, part-time"}, "part_time"),
    ("rippling", {"label": "HOURLY_FT", "id": "Hourly, full-time"}, "full_time"),
    ("rippling", {"label": "HOURLY_PT", "id": "Hourly, part-time"}, "part_time"),
    ("rippling", {"label": "CONTRACTOR", "id": "Contractor"}, "contract"),
    ("rippling", {"label": "TEMP", "id": "Temp"}, "temporary"),
    ("rippling", {"label": "INTERN", "id": "Intern"}, "internship"),
]


@pytest.mark.parametrize(("provider", "value", "expected"), VOCABULARIES)
def test_provider_vocabularies(provider, value, expected):
    employment, raw, source = detect_employment(value)
    assert employment == expected, provider
    assert source == "ats"
    assert raw == (value["id"] if isinstance(value, dict) else value)


def test_rippling_reading_only_label_would_have_broken_every_job():
    # ``.label`` alone canonicalizes to "salariedft"; the map carries both spellings so
    # neither reading falls through to ``other``.
    assert detect_employment({"label": "SALARIED_FT"})[0] == "full_time"
    assert detect_employment("Salaried, full-time")[0] == "full_time"


def test_unknown_value_is_other_with_the_original_preserved():
    employment, raw, source = detect_employment("Seasonal Weekend Crew")
    assert (employment, raw, source) == ("other", "Seasonal Weekend Crew", "ats")


def test_personio_schedule_refines_part_time():
    assert detect_employment("permanent", None, "part-time")[0] == "part_time"
    assert detect_employment("permanent", None, "full-time")[0] == "full_time"
    # A schedule may not overrule a more specific type.
    assert detect_employment("intern", None, "part-time")[0] == "internship"


TITLES = [
    ("Software Engineering Intern", "internship"),
    ("Internship: Data Science", "internship"),
    ("Praktikum Marketing", "internship"),
    ("Werkstudent Vertrieb (m/w/d)", "internship"),
    ("Working Student - Finance", "internship"),
    ("Freelance Motion Designer", "contract"),
    ("Contractor - Platform", "contract"),
    ("Senior Developer (B2B)", "contract"),
    ("Part-Time Barista", "part_time"),
    ("Part time Support Agent", "part_time"),
    ("Maternity Cover - Office Manager", "temporary"),
    ("Fixed-term Analyst", "temporary"),
    ("Temporary Warehouse Associate", "temporary"),
    ("Senior Backend Engineer", None),
]


@pytest.mark.parametrize(("title", "expected"), TITLES)
def test_title_fallback(title, expected):
    assert employment_from_title(title) == expected


def test_greenhouse_has_no_field_so_the_title_decides_and_says_so():
    employment, raw, source = detect_employment(None, "Data Science Intern")
    assert (employment, raw, source) == ("internship", None, "title")


def test_strict_employment_type_can_tell_the_two_apart():
    # ``strictEmploymentType`` == keep only ``employmentTypeSource == "ats"`` (§4.5.4).
    ats = detect_employment("Full-time", "Full-time Engineer")
    title = detect_employment(None, "Full-time Engineer")
    assert ats[2] == "ats"
    assert title[2] != "ats"


@pytest.mark.parametrize("value", [None, "", "   ", {}, {"label": None}, 7, []])
def test_nothing_reported_and_nothing_in_the_title_is_null_not_other(value):
    assert detect_employment(value, "Senior Backend Engineer") == (None, None, None)
