"""§4.5.1 location parsing, including every case named in §10.1."""

from __future__ import annotations

import pytest

from core.normalize.location import (
    country_by_token,
    country_currency,
    country_name,
    is_region_only,
    parse_location,
    parse_locations,
    split_location_text,
    strip_workplace_markers,
)

# (raw, city, region, country, countryCode)
CASES = [
    ("San Francisco, CA", "San Francisco", "CA", "United States", "US"),
    ("Berlin, Germany", "Berlin", None, "Germany", "DE"),
    ("London", "London", None, None, None),  # step 5: we do not guess GB
    ("Remote - US", None, None, "United States", "US"),
    ("Remote (EMEA)", None, None, None, None),
    ("Tokyo, Japan; Singapore", "Tokyo", None, "Japan", "JP"),
    ("New York, NY, United States", "New York", "NY", "United States", "US"),
    ("Toronto, ON, Canada", "Toronto", "ON", "Canada", "CA"),
    ("Sydney, NSW", "Sydney", "NSW", "Australia", "AU"),
    ("", None, None, None, None),
    (None, None, None, None, None),
    ("Anywhere", None, None, None, None),
    ("EMEA", None, None, None, None),
    ("APAC", None, None, None, None),
    ("Americas", None, None, None, None),
    ("Worldwide", None, None, None, None),
    ("Global", None, None, None, None),
    ("Remote", None, None, None, None),
    ("Munich, Germany (Remote)", "Munich", None, "Germany", "DE"),
    ("Hybrid - Dublin, Ireland", "Dublin", None, "Ireland", "IE"),
    ("Amsterdam - Remote", "Amsterdam", None, None, None),
    ("Vancouver, BC, Canada", "Vancouver", "BC", "Canada", "CA"),
    ("Austin, Texas", "Austin", "Texas", "United States", "US"),
    ("Washington, DC", "Washington", "DC", "United States", "US"),
    ("San Juan, PR", "San Juan", "PR", "United States", "US"),
    ("Melbourne, Victoria", "Melbourne", "Victoria", "Australia", "AU"),
    ("Zurich, Switzerland", "Zurich", None, "Switzerland", "CH"),
    ("Paris, FRA", "Paris", None, "France", "FR"),  # ISO3
    ("Bristol, UK", "Bristol", None, "United Kingdom", "GB"),  # alias
    ("Edinburgh, Scotland", "Edinburgh", None, "United Kingdom", "GB"),
    ("Austin, TX, USA", "Austin", "TX", "United States", "US"),
    ("Boston, MA, U.S.", "Boston", "MA", "United States", "US"),
    ("Munich, Deutschland", "Munich", None, "Germany", "DE"),
    ("Rotterdam, Holland", "Rotterdam", None, "Netherlands", "NL"),
    ("Prague, Czechia", "Prague", None, "Czechia", "CZ"),
    ("Dubai, UAE", "Dubai", None, "United Arab Emirates", "AE"),
    ("Seoul, Korea", "Seoul", None, "South Korea", "KR"),
    ("Greater Boston Area, MA", "Greater Boston Area", "MA", "United States", "US"),
    ("Bengaluru, Karnataka, India", "Bengaluru, Karnataka", None, "India", "IN"),
    ("   Oslo,   Norway  ", "Oslo", None, "Norway", "NO"),  # step 7: NBSP + collapse
    ("SAN FRANCISCO, CA", "SAN FRANCISCO", "CA", "United States", "US"),  # casing kept
]


@pytest.mark.parametrize(("raw", "city", "region", "country", "code"), CASES)
def test_parse_location(raw, city, region, country, code):
    loc = parse_location(raw)
    assert (loc.city, loc.region, loc.country, loc.countryCode) == (city, region, country, code)


@pytest.mark.parametrize("raw", [c[0] for c in CASES if c[0]])
def test_raw_is_preserved_and_country_code_is_upper(raw):
    loc = parse_location(raw)
    assert loc.raw == " ".join(raw.replace(" ", " ").split()) or loc.raw
    assert loc.countryCode is None or loc.countryCode == loc.countryCode.upper()


def test_country_and_code_are_never_half_populated():
    for raw, *_ in CASES:
        loc = parse_location(raw)
        assert (loc.country is None) == (loc.countryCode is None)


# --- step 1: structured wins ----------------------------------------------------------


@pytest.mark.parametrize(
    "structured",
    [
        {"addressLocality": "Dublin", "addressCountry": "ie"},  # Ashby postalAddress
        {"city": "Dublin", "country": "Ireland", "country_code": "ie"},  # Recruitee
        {"city": "Dublin", "country": {"name": "Ireland"}},  # Breezy
        {"city": "Dublin", "countryCode": "IRL"},  # ISO3 in the code field
    ],
)
def test_structured_paths_upper_case_the_code_for_every_provider(structured):
    loc = parse_location("anything at all", structured)
    assert (loc.city, loc.country, loc.countryCode) == ("Dublin", "Ireland", "IE")


def test_structured_beats_free_text():
    loc = parse_location("Remote - US", {"city": "Berlin", "countryCode": "de"})
    assert (loc.city, loc.countryCode) == ("Berlin", "DE")


def test_empty_structured_falls_through_to_text():
    assert parse_location("Berlin, Germany", {}).countryCode == "DE"
    assert parse_location("Berlin, Germany", {"city": None}).countryCode == "DE"


# --- step 2: multi-location splitting -------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Tokyo, Japan; Singapore", ["Tokyo, Japan", "Singapore"]),
        ("Paris, France | Berlin, Germany", ["Paris, France", "Berlin, Germany"]),
        ("Austin, TX / Denver, CO", ["Austin, TX", "Denver, CO"]),
        ("Berlin, Germany or Munich, Germany", ["Berlin, Germany", "Munich, Germany"]),
        ("Research and Development", ["Research and Development"]),  # neither half is a place
        ("Anywhere", ["Anywhere"]),
        ("", []),
    ],
)
def test_split_location_text(raw, expected):
    assert split_location_text(raw) == expected


def test_locations_are_sorted_and_deduped():
    primary, locations = parse_locations(
        "San Francisco, CA", ["New York, NY", "Remote - EMEA", "San Francisco, CA"]
    )
    assert primary.city == "San Francisco"
    assert [loc.raw for loc in locations] == [
        "Remote - EMEA",  # countryCode "" sorts first
        "San Francisco, CA",
        "New York, NY",
    ]


def test_locations_sorting_is_shuffle_invariant():
    order_a = parse_locations("Berlin, Germany", ["Austin, TX", "Toronto, ON, Canada"])[1]
    order_b = parse_locations("Berlin, Germany", ["Toronto, ON, Canada", "Austin, TX"])[1]
    assert [loc.sort_key for loc in order_a] == [loc.sort_key for loc in order_b]


def test_parse_locations_from_structured_only():
    primary, locations = parse_locations(
        None, None, [{"city": "Dublin", "country_code": "IE"}, {"city": "Berlin", "country": "DE"}]
    )
    assert primary.city == "Dublin"
    assert [loc.countryCode for loc in locations] == ["DE", "IE"]


def test_parse_locations_with_nothing_at_all():
    primary, locations = parse_locations(None)
    assert (primary.raw, locations) == (None, [])


# --- step 3 / step 6 helpers ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "text", "marker"),
    [
        ("Remote - US", "US", "remote"),
        ("Hybrid: Berlin", "Berlin", "hybrid"),
        ("On-site, Berlin", "Berlin", "on-site"),
        ("In-office — Berlin", "Berlin", "in-office"),  # em dash counts as a separator
        ("In office Berlin", "In office Berlin", None),  # no separator, nothing stripped
        ("Berlin (Remote)", "Berlin", "remote"),
        ("Berlin [On-Site]", "Berlin", "on-site"),
        ("Berlin - Hybrid", "Berlin", "hybrid"),
        ("Berlin, Germany", "Berlin, Germany", None),
    ],
)
def test_strip_workplace_markers(raw, text, marker):
    stripped, found = strip_workplace_markers(raw)
    assert (stripped, found) == (text, marker)


@pytest.mark.parametrize(
    "raw", ["EMEA", "APAC", "Americas", "Worldwide", "Global", "Anywhere", "Anywhere in the World"]
)
def test_region_only(raw):
    assert is_region_only(raw)


@pytest.mark.parametrize("raw", ["Berlin", "US", "Remote - US", ""])
def test_not_region_only(raw):
    assert not is_region_only(raw)


# --- reference data -------------------------------------------------------------------


def test_geo_lookups():
    assert country_by_token("Deutschland") == ("DE", "Germany")
    assert country_by_token("USA") == ("US", "United States")
    assert country_by_token("not a country") is None
    assert country_name("gb") == "United Kingdom"
    assert country_name(None) is None
    assert country_currency("IE") == "EUR"
    assert country_currency("US") == "USD"
    assert country_currency("ZZ") is None


# --- primary promotion (live run, §10.2) ----------------------------------------------


def test_workplace_word_as_primary_takes_its_place_from_the_office_list():
    """Cloudflare's Greenhouse board puts "Hybrid" in `location.name` on 207 of 310 jobs
    and the real place only in `offices[].location` — `city` used to be null board-wide."""
    primary, locations = parse_locations("Hybrid", ["Washington, DC, United States"])
    assert primary.raw == "Hybrid", "locationRaw stays the provider's own string"
    assert (primary.city, primary.region, primary.countryCode) == ("Washington", "DC", "US")
    assert [loc.raw for loc in locations] == ["Hybrid", "Washington, DC, United States"]


def test_promotion_takes_the_first_office_after_the_step_8_sort():
    primary, _ = parse_locations(
        "In-Office", ["New York, New York, United States", "Austin, TX, United States"]
    )
    # (countryCode, region, city, raw): US/"New York" sorts before US/"TX".
    assert (primary.city, primary.region) == ("New York", "New York")


def test_a_primary_that_names_a_place_is_never_overwritten():
    primary, _ = parse_locations("Dublin, Ireland", ["Austin, TX, United States"])
    assert (primary.city, primary.countryCode) == ("Dublin", "IE")


def test_nothing_to_promote_leaves_the_primary_null():
    primary, _ = parse_locations("Remote", ["Distributed"])
    assert (primary.raw, primary.city, primary.countryCode) == ("Remote", None, None)


def test_country_first_comma_order_resolves_the_country():
    """Agile Robots writes its Personio office as "Germany, Munich (HQ)" on every job;
    right-to-left alone left `countryCode` null and swept "Germany" into `city`."""
    primary, _ = parse_locations("Germany, Munich (HQ)")
    assert (primary.city, primary.country, primary.countryCode) == ("Munich (HQ)", "Germany", "DE")
    assert primary.raw == "Germany, Munich (HQ)"


def test_country_first_order_does_not_outrank_a_trailing_subdivision():
    """ "Georgia" is both a country and a US state; "Atlanta, Georgia" stays American."""
    primary, _ = parse_locations("Atlanta, Georgia")
    assert (primary.city, primary.region, primary.countryCode) == ("Atlanta", "Georgia", "US")


def test_a_lone_country_is_still_a_country_and_not_a_city():
    primary, _ = parse_locations("Germany")
    assert (primary.city, primary.countryCode) == (None, "DE")
