"""§4.5.3 salary parsing: structured per provider, the regex fallback, and the gates.

A wrong salary is worse than no salary (R13), so the rejection cases carry as much weight
here as the extraction cases.
"""

from __future__ import annotations

import pytest

from core.models import Location
from core.normalize.salary import (
    ashby_interval,
    normalize_interval,
    parse_salary,
    parse_salary_text,
    structured_salary,
)

# --- step 1: structured ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1 YEAR", "year"),  # the only value the live openai board emits, 1279 times
        ("1 HOUR", "hour"),
        ("1 MONTH", "month"),
        ("1 WEEK", "week"),
        ("1 DAY", "day"),
        ("2 WEEKS", None),  # not a period we normalize
        ("3 MONTHS", None),
        ("YEAR", "year"),  # tolerate a bare unit
        ("PER_YEAR", None),  # v1's mapping; it appears nowhere in the live data
        (None, None),
        ("", None),
    ],
)
def test_ashby_interval(raw, expected):
    assert ashby_interval(raw) == expected


def test_ashby_structured():
    job = {
        "compensation": {
            "compensationTierSummary": "$300K – $405K • Offers Equity",
            "summaryComponents": [
                {
                    "compensationType": "Salary",
                    "minValue": 300000,
                    "maxValue": 405000,
                    "currencyCode": "USD",
                    "interval": "1 YEAR",
                },
                {"compensationType": "EquityPercentage", "minValue": 0.1, "maxValue": 0.2},
            ],
        }
    }
    salary = parse_salary(job)
    assert (salary.min, salary.max, salary.currency, salary.interval) == (
        300000,
        405000,
        "USD",
        "year",
    )
    assert salary.source == "ats"
    assert salary.raw == "$300K – $405K • Offers Equity"


def test_ashby_two_weeks_keeps_the_money_and_nulls_only_the_interval():
    job = {
        "compensation": {
            "summaryComponents": [
                {
                    "compensationType": "Salary",
                    "minValue": 4000,
                    "maxValue": 5000,
                    "currencyCode": "EUR",
                    "interval": "2 WEEKS",
                }
            ]
        }
    }
    salary = parse_salary(job)
    assert (salary.min, salary.max, salary.currency) == (4000, 5000, "EUR")
    assert salary.interval is None and salary.source == "ats"


def test_ashby_widest_range_across_salary_tiers():
    job = {
        "compensation": {
            "summaryComponents": [
                {
                    "compensationType": "Salary",
                    "minValue": 120000,
                    "maxValue": 150000,
                    "currencyCode": "USD",
                    "interval": "1 YEAR",
                },
                {
                    "compensationType": "Salary",
                    "minValue": 100000,
                    "maxValue": 180000,
                    "currencyCode": "USD",
                    "interval": "1 YEAR",
                },
            ]
        }
    }
    salary = parse_salary(job)
    assert (salary.min, salary.max) == (100000, 180000)


@pytest.mark.parametrize(
    ("interval", "expected"),
    [("per-year-salary", "year"), ("per-hour-wage", "hour"), ("per-month", "month")],
)
def test_lever_structured(interval, expected):
    job = {
        "salaryRange": {"min": 90000, "max": 120000, "currency": "GBP", "interval": interval},
        "salaryDescription": "Competitive, plus equity",
    }
    salary = parse_salary(job)
    assert (salary.min, salary.max, salary.currency, salary.interval) == (
        90000,
        120000,
        "GBP",
        expected,
    )
    assert salary.source == "ats" and salary.raw == "Competitive, plus equity"


def test_greenhouse_cents_and_default_interval():
    job = {
        "pay_input_ranges": [
            {
                "min_cents": 18000000,
                "max_cents": 24000000,
                "currency_type": "USD",
                "title": "Pay Range",
            }
        ]
    }
    salary = parse_salary(job)
    assert (salary.min, salary.max, salary.interval, salary.source) == (
        180000,
        240000,
        "year",
        "ats",
    )
    assert salary.raw is None  # a single range needs no disambiguation


GH_MULTI = [
    {
        "min_cents": 18000000,
        "max_cents": 24000000,
        "currency_type": "USD",
        "title": "US Annual Pay Range",
        "blurb": "",
    },
    {
        "min_cents": 9000000,
        "max_cents": 11000000,
        "currency_type": "EUR",
        "title": "Ireland Annual Pay Range",
        "blurb": "",
    },
]


def test_greenhouse_multi_range_prefers_the_locale_in_the_title():
    location = Location(raw="Dublin, Ireland", city="Dublin", country="Ireland", countryCode="IE")
    salary = parse_salary({"pay_input_ranges": GH_MULTI}, None, location)
    assert (salary.min, salary.max, salary.currency) == (90000, 110000, "EUR")
    assert salary.raw is None


def test_greenhouse_multi_range_falls_back_to_the_country_currency():
    location = Location(raw="Berlin", city="Berlin", country="Germany", countryCode="DE")
    salary = parse_salary({"pay_input_ranges": GH_MULTI}, None, location)
    assert salary.currency == "EUR"


def test_greenhouse_multi_range_last_resort_records_which_locale_it_took():
    location = Location(raw="Tokyo", city="Tokyo", country="Japan", countryCode="JP")
    salary = parse_salary({"pay_input_ranges": GH_MULTI}, None, location)
    assert (salary.currency, salary.raw) == ("USD", "US Annual Pay Range")


def test_greenhouse_hourly_range_from_its_own_title():
    job = {
        "pay_input_ranges": [
            {
                "min_cents": 4500,
                "max_cents": 6000,
                "currency_type": "USD",
                "title": "Hourly Pay Range",
            }
        ]
    }
    assert parse_salary(job).interval == "hour"


def test_recruitee_structured():
    salary = parse_salary(
        {"salary": {"min": 50000, "max": 60000, "currency": "EUR", "period": "year"}}
    )
    assert (salary.min, salary.max, salary.currency, salary.interval, salary.source) == (
        50000,
        60000,
        "EUR",
        "year",
        "ats",
    )


def test_rippling_structured():
    job = {
        "payRangeDetails": [
            {"min": 100000, "max": 130000, "currency": "USD", "frequency": "YEARLY"}
        ]
    }
    salary = parse_salary(job)
    assert (salary.min, salary.max, salary.interval, salary.source) == (
        100000,
        130000,
        "year",
        "ats",
    )


@pytest.mark.parametrize(
    "job",
    [
        {},
        {"compensation": {}},
        {"compensation": {"summaryComponents": []}},
        {"salaryRange": {}},
        {"salary": {}},
        {"pay_input_ranges": []},
        {"payRangeDetails": []},
    ],
)
def test_empty_provider_objects_never_raise(job):
    salary = parse_salary(job)
    assert (salary.min, salary.max, salary.source) == (None, None, None)


def test_structured_salary_returns_none_when_there_is_nothing():
    assert structured_salary({}) is None


# --- step 2: regex fallback -----------------------------------------------------------

PARSED = [
    ("$180,000 - $240,000 per year", 180000, 240000, "USD", "year"),
    ("€60k–€80k", 60000, 80000, "EUR", "year"),
    ("£45,000 to £55,000 per annum", 45000, 55000, "GBP", "year"),
    ("$45 - $60 / hour", 45, 60, "USD", "hour"),
    ("120000-160000 USD annually", 120000, 160000, "USD", "year"),
    ("CA$90,000 - CA$110,000 per year", 90000, 110000, "CAD", "year"),
    ("A$120,000 and A$140,000 yearly", 120000, 140000, "AUD", "year"),
    ("S$8,000 - S$10,000 per month", 8000, 10000, "SGD", "month"),
    ("₹1,500,000 until ₹2,000,000 p.a.", 1500000, 2000000, "INR", "year"),
    ("Salary: 50 000 - 70 000 EUR per year", 50000, 70000, "EUR", "year"),
    ("Rate: 300 - 450 per day", 300, 450, None, "day"),
    ("$25 to $35 hourly", 25, 35, "USD", "hour"),
    ("90k - 120k", 90000, 120000, None, "year"),  # k-suffix implies a yearly figure
    ("$20 - $30", 20, 30, "USD", "hour"),  # magnitude implies an hourly figure
    # An ISO code trailing the interval wording, past `iso3`'s reach. Live on the Lever
    # palantir board, where the currency was silently dropped before (§10.2).
    (
        "The salary range is estimated to be 110,000 - 200,000/year SGD",
        110000,
        200000,
        "SGD",
        "year",
    ),
]


@pytest.mark.parametrize(("text", "low", "high", "currency", "interval"), PARSED)
def test_regex_fallback(text, low, high, currency, interval):
    salary = parse_salary_text(text)
    assert salary is not None, text
    assert (salary.min, salary.max, salary.currency, salary.interval) == (
        low,
        high,
        currency,
        interval,
    )
    assert salary.source == "parsed"


REJECTED = [
    "We raised $50M Series B last year",
    "401(k) with 4% match",
    "The 2020 - 2024 strategy",
    "$10 - $5,000,000",  # max/min > 20
    "up to $10,000 bonus",
    "equity of 100,000 - 200,000 options",
    "revenue grew 500,000 - 900,000 last year",
    "$1 - $10 per hour",  # hour interval with min < 2
    "$3,000 - $4,000 per hour",  # hour interval with max > 2,000
    "$100 - $900 per year",  # year interval with min < 1,000
    "$1,000,000 - $9,000,000 per year",  # year interval with max > 5,000,000
    "travel 40% - 60% of the time",
    "call +1 555-123-4567 to apply",
    "reference 8130725 - 8130999",  # ids, not money
    # Real false positives from the §10.2 live run. Each passed every gate §4.5.3 lists
    # and shipped a wrong salary; a bare range with no currency and no `k` is not money.
    "a combined org of 60–100+ Stripes",  # was $60–100/hour
    "a global marketing operations team of 15-20, in addition to 25-30 specialists",
    "influencing results over the next 6–24 months",  # was $6–24/hour
    "dedicating a total of 5 to 10 hours per week to instruction",  # was 5–10/week
    "",
    None,
]


@pytest.mark.parametrize("text", REJECTED)
def test_rejection_gates(text):
    assert parse_salary_text(text) is None


def test_min_greater_than_max_is_rejected():
    assert parse_salary_text("$240,000 - $180,000 per year") is None


def test_only_three_candidates_are_evaluated():
    # Three junk ranges the gates reject, then a real one that is never reached.
    text = "2001 - 2005, 2006 - 2010, 2011 - 2015, and the salary is $180,000 - $240,000 per year"
    assert parse_salary_text(text) is None


def test_free_text_field_is_searched_before_the_description():
    job = {"salaryDescription": "£45,000 to £55,000 per annum"}
    salary = parse_salary(job, "The role pays $180,000 - $240,000 per year")
    assert (salary.currency, salary.min) == ("GBP", 45000)
    assert salary.raw == "£45,000 to £55,000 per annum"


def test_description_is_capped_at_4000_characters():
    head = "x" * 4100 + " $180,000 - $240,000 per year"
    assert parse_salary({}, head).source is None


def test_step_three_keeps_the_provider_free_text():
    job = {"salaryDescription": "Competitive salary, DOE"}
    salary = parse_salary(job)
    assert (salary.min, salary.source, salary.raw) == (None, None, "Competitive salary, DOE")


def test_normalize_interval_gives_up_rather_than_guessing():
    assert normalize_interval("fortnightly") is None
    assert normalize_interval(None) is None
    assert normalize_interval(7) is None
