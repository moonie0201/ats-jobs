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
    ("interval", "expected", "low", "high"),
    [
        ("per-year-salary", "year", 90000, 120000),
        ("per-hour-wage", "hour", 45, 60),
        ("per-month", "month", 7500, 10000),
    ],
)
def test_lever_structured(interval, expected, low, high):
    """Amounts match their label here; the contradiction cases are below."""
    job = {
        "salaryRange": {"min": low, "max": high, "currency": "GBP", "interval": interval},
        "salaryDescription": "Competitive, plus equity",
    }
    salary = parse_salary(job)
    assert (salary.min, salary.max, salary.currency, salary.interval) == (
        low,
        high,
        "GBP",
        expected,
    )
    assert salary.source == "ats" and salary.raw == "Competitive, plus equity"


def test_lever_zero_band_is_absent_pay_not_a_job_paying_nothing():
    """A Lever employer can switch the field on and leave it at zero (seeker-os#35). That
    must not be published as a declared range."""
    job = {
        "salaryRange": {"min": 0, "max": 0, "currency": "USD", "interval": "per-year-salary"},
        "salaryDescription": "Competitive",
    }
    salary = parse_salary(job)
    assert (salary.min, salary.max, salary.source) == (None, None, None)
    assert salary.raw == "Competitive"
    # With no text either, nothing at all is reported: `parse_salary` returns an empty
    # Salary rather than None, so assert on the fields.
    empty = parse_salary({"salaryRange": {"min": 0, "max": 0, "currency": "USD"}})
    assert (empty.min, empty.max, empty.currency, empty.interval, empty.source, empty.raw) == (
        None,
        None,
        None,
        None,
        None,
        None,
    )


def test_lever_zero_min_with_a_real_max_is_kept():
    salary = parse_salary(
        {"salaryRange": {"min": 0, "max": 150000, "currency": "USD", "interval": "per-year-salary"}}
    )
    assert (salary.min, salary.max, salary.interval, salary.source) == (0, 150000, "year", "ats")


@pytest.mark.parametrize(
    ("low", "high", "interval", "expected"),
    [
        # Live gopuff advert: an hourly band labelled bi-weekly (seeker-os#35). $26 a week
        # is below any real wage, so the label is not evidence and the amount is kept as is.
        (22.4, 26, "bi-week-salary", None),
        # The same magnitude with an honest label survives.
        (22.4, 26, "per-hour-wage", "hour"),
        # A real weekly band is believed.
        (1200, 1500, "bi-week-salary", "week"),
        # An annual magnitude cannot be an hourly rate.
        (90000, 120000, "per-hour-wage", None),
    ],
)
def test_lever_interval_label_is_tested_against_the_amount(low, high, interval, expected):
    salary = parse_salary(
        {"salaryRange": {"min": low, "max": high, "currency": "USD", "interval": interval}}
    )
    assert (salary.min, salary.max) == (low, high)  # the amount is never rescaled
    assert salary.interval == expected


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


# --- Greenhouse label-vs-magnitude coherence (live run, §10.2) -------------------------

#: Verkada's real shape: an incoherent "hourly" band carrying an annual number, and the
#: real annual band beside it. `ranges[0]` used to win, publishing $200,000 per hour.
VERKADA_MISLABELLED = [
    {
        "min_cents": 20000000,
        "max_cents": 26000000,
        "currency_type": "USD",
        "title": "Estimated Hourly Pay Range",
    },
    {
        "min_cents": 16000000,
        "max_cents": 28000000,
        "currency_type": "USD",
        "title": "Estimated Annual Pay Range",
    },
]

#: Verkada again: a $1.00 placeholder in the hourly band, the truth in the annual one.
VERKADA_PLACEHOLDER = [
    {
        "min_cents": 100,
        "max_cents": 100,
        "currency_type": "USD",
        "title": "Estimated Hourly Pay Range",
    },
    {
        "min_cents": 22500000,
        "max_cents": 26500000,
        "currency_type": "USD",
        "title": "Estimated Annual Pay Range",
    },
]


def test_greenhouse_hourly_label_on_an_annual_number_is_not_believed():
    salary = parse_salary({"pay_input_ranges": VERKADA_MISLABELLED}, None, None)
    assert (salary.min, salary.max, salary.interval) == (160000, 280000, "year")


def test_greenhouse_placeholder_band_loses_to_the_real_one():
    salary = parse_salary({"pay_input_ranges": VERKADA_PLACEHOLDER}, None, None)
    assert (salary.min, salary.max, salary.interval) == (225000, 265000, "year")


def test_greenhouse_unlabelled_hourly_wage_is_not_called_a_year():
    """Rocket Lab: `min_cents: 1885` under "Base Pay Range (MD Only)" — no interval word.
    The old `or "year"` default published a $18.85-per-year driver."""
    band = [
        {
            "min_cents": 1885,
            "max_cents": 2350,
            "currency_type": "USD",
            "title": "Base Pay Range (MD Only)",
        }
    ]
    salary = parse_salary({"pay_input_ranges": band}, None, None)
    assert (salary.min, salary.max, salary.interval) == (18.85, 23.5, "hour")


def test_greenhouse_unlabelled_annual_salary_still_reads_year():
    band = [
        {
            "min_cents": 18000000,
            "max_cents": 24000000,
            "currency_type": "USD",
            "title": "Base Pay Range",
        }
    ]
    assert parse_salary({"pay_input_ranges": band}, None, None).interval == "year"


def test_greenhouse_lone_incoherent_band_keeps_the_numbers_and_drops_the_interval():
    """Verkada's "Head of Government Affairs": $315-$400 under an *annual* label. We do
    not know what it means, so the interval is null rather than "$315 a year"."""
    band = [
        {
            "min_cents": 31500,
            "max_cents": 40000,
            "currency_type": "USD",
            "title": "Estimated Annual Pay Range",
        }
    ]
    salary = parse_salary({"pay_input_ranges": band}, None, None)
    assert (salary.min, salary.max, salary.interval, salary.source) == (315, 400, None, "ats")


# --- V1 H4: a compensation object with no numbers is a failed step 1 ------------------


@pytest.mark.parametrize(
    "job",
    [
        {"salaryRange": {"currency": "USD"}},  # lever
        {"salaryRange": {"currency": "USD", "min": None, "max": None, "interval": "per-year"}},
        {"pay_input_ranges": [{"currency_type": "USD", "title": "Annual"}]},  # greenhouse
        {"payRangeDetails": [{"currency": "USD"}]},  # rippling
        {"salary": {"currency": "EUR", "period": "yearly"}},  # recruitee
    ],
)
def test_an_empty_compensation_object_never_claims_the_ats_source(job):
    """§4.5.3 step 3: nulls beat a wrong provenance, and step 1 must not block step 2."""
    found = structured_salary(job)
    assert found is None or found.source is None, found


def test_an_empty_compensation_object_lets_the_regex_fallback_run():
    """`parse_salary` short-circuits on `source == "ats"`; that used to suppress step 2."""
    parsed = parse_salary(
        {"salaryRange": {"currency": "USD"}}, "We pay $120,000 - $150,000 per year."
    )
    assert (parsed.min, parsed.max, parsed.source) == (120000.0, 150000.0, "parsed")


def test_a_populated_compensation_object_still_claims_the_ats_source():
    parsed = parse_salary({"salaryRange": {"currency": "USD", "min": 100000, "max": 120000}})
    assert parsed.source == "ats" and parsed.min == 100000


# --- Greenhouse indistinguishable bands (live run, §10.2) -----------------------------

#: Databricks' real shape on 139 of 824 jobs: four zone bands, one currency, one
#: interval, no place anywhere in the label, and `location.name` == "United States".
DATABRICKS_ZONES = [
    {
        "min_cents": 14660000,
        "max_cents": 20165000,
        "currency_type": "USD",
        "title": "Zone 1 Pay Range",
        "blurb": "<p>Databricks is committed to fair pay.</p>",
    },
    {
        "min_cents": 13200000,
        "max_cents": 18150000,
        "currency_type": "USD",
        "title": "Zone 2 Pay Range",
        "blurb": "",
    },
    {
        "min_cents": 12460000,
        "max_cents": 17140000,
        "currency_type": "USD",
        "title": "Zone 3 Pay Range",
        "blurb": "",
    },
    {
        "min_cents": 11730000,
        "max_cents": 16125000,
        "currency_type": "USD",
        "title": "Zone 4 Pay Range",
        "blurb": "",
    },
]


def test_greenhouse_indistinguishable_bands_are_spanned_not_picked():
    """`ranges[0]` published Zone 1 — the top band — as *the* salary and overstated the
    floor by 25%. Nothing in the payload says which zone the job is in, so the span is
    the only true statement about it."""
    location = Location(raw="United States", country="United States", countryCode="US")
    salary = parse_salary({"pay_input_ranges": DATABRICKS_ZONES}, None, location)
    assert (salary.min, salary.max) == (117300, 201650)
    assert (salary.currency, salary.interval, salary.source) == ("USD", "year", "ats")
    assert salary.raw == "Zone 1 Pay Range / Zone 2 Pay Range / Zone 3 Pay Range / Zone 4 Pay Range"


def test_greenhouse_currency_tiebreak_narrows_before_it_spans():
    """A USD band beside two EUR ones: the EUR pair is what a German job may be paid,
    and the USD band must not be spanned into it."""
    ranges = [
        {"min_cents": 18000000, "max_cents": 24000000, "currency_type": "USD", "title": "US"},
        {"min_cents": 9000000, "max_cents": 11000000, "currency_type": "EUR", "title": "EU A"},
        {"min_cents": 8000000, "max_cents": 10000000, "currency_type": "EUR", "title": "EU B"},
    ]
    location = Location(raw="Berlin", city="Berlin", country="Germany", countryCode="DE")
    salary = parse_salary({"pay_input_ranges": ranges}, None, location)
    assert (salary.min, salary.max, salary.currency) == (80000, 110000, "EUR")


def test_greenhouse_a_named_locale_still_wins_outright_over_the_span():
    location = Location(raw="Dublin, Ireland", city="Dublin", country="Ireland", countryCode="IE")
    salary = parse_salary({"pay_input_ranges": GH_MULTI}, None, location)
    assert (salary.min, salary.max, salary.raw) == (90000, 110000, None)


def test_greenhouse_bands_that_disagree_on_interval_are_never_spanned():
    """Spanning an hourly band into an annual one would invent a 1:4000 range."""
    ranges = [
        {"min_cents": 5100, "max_cents": 6000, "currency_type": "USD", "title": "Hourly Rate"},
        {
            "min_cents": 14660000,
            "max_cents": 20165000,
            "currency_type": "USD",
            "title": "Annual Pay Range",
        },
    ]
    salary = parse_salary({"pay_input_ranges": ranges}, None, None)
    assert (salary.min, salary.max, salary.interval) == (51, 60, "hour")
    assert salary.raw == "Hourly Rate"


# --- V1 H5: step 1 had no sanity gates at all -----------------------------------------


def test_ashby_never_spans_two_different_intervals():
    """V1 H5: `_ashby` widened min/max across every Salary component while reading
    currency and interval off `components[0]`, so a posting carrying both an hourly and an
    annual band shipped `min=25 max=200000 interval='hour'` labelled `salarySource: "ats"`
    — on the provider the README calls the cleanest of the six."""
    job = {
        "compensation": {
            "summaryComponents": [
                {
                    "compensationType": "Salary",
                    "minValue": 25,
                    "maxValue": 60,
                    "currencyCode": "USD",
                    "interval": "1 HOUR",
                },
                {
                    "compensationType": "Salary",
                    "minValue": 150000,
                    "maxValue": 200000,
                    "currencyCode": "USD",
                    "interval": "1 YEAR",
                },
                {
                    "compensationType": "Salary",
                    "minValue": 160000,
                    "maxValue": 210000,
                    "currencyCode": "USD",
                    "interval": "1 YEAR",
                },
            ]
        }
    }
    salary = structured_salary(job)
    assert (salary.min, salary.max) == (150000.0, 210000.0)
    assert (salary.interval, salary.currency, salary.source) == ("year", "USD", "ats")


def test_an_implausible_structured_band_is_not_published_as_the_ats_answer():
    """V1 H5: §4.5.3's ratio gate lived in `_rejected`, which runs on the regex path only,
    so step 1 was ungated for all six providers. R13 — a wrong salary is worse than none."""
    absurd = {"salaryRange": {"min": 25, "max": 200000, "currency": "USD", "interval": "year"}}
    assert parse_salary(absurd).source is None
    honest = {"salaryRange": {"min": 150000, "max": 200000, "currency": "USD", "interval": "year"}}
    assert parse_salary(honest).source == "ats"


def test_a_real_high_magnitude_currency_still_ships():
    """The shared gate is currency-agnostic on purpose: Ashby publishes a genuine
    `COP 248M - COP 310M` per year (about $60k USD), and a USD-shaped absolute ceiling
    would delete it. Only a band that contradicts *itself* is rejected at step 1."""
    job = {
        "compensation": {
            "summaryComponents": [
                {
                    "compensationType": "Salary",
                    "minValue": 248_000_000,
                    "maxValue": 310_000_000,
                    "currencyCode": "COP",
                    "interval": "1 YEAR",
                }
            ]
        }
    }
    assert structured_salary(job).source == "ats"
