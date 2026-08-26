"""Location parsing (SPEC v2 §4.5.1).

Deterministic, offline, table-driven. No geocoding, no lat/lng, no external service: the
only reference data is ``core/data/geo.json`` (~18 KB, ISO 3166-1 plus the US/CA/AU
subdivisions, hand-maintainable).

Where a rule cannot decide, the field stays ``None``. ``"London"`` alone yields
``city="London"`` and a null country — we do not guess GB.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib.resources import files
from typing import Any

from core.models import Location

# --- reference data -------------------------------------------------------------------


@lru_cache(maxsize=1)
def _geo() -> dict[str, Any]:
    """``core/data/geo.json``, loaded once on first use (§4.5.1 step 9).

    Addressed through the ``core`` package, not ``core.data``: ``core/data`` holds no
    ``__init__.py``, so it ships as package *data* (pyproject ``package-data``) and is not
    importable in an installed wheel.
    """
    return json.loads((files("core") / "data" / "geo.json").read_text(encoding="utf-8"))


def _key(value: str | None) -> str:
    """Lookup key: casefold and drop everything that is not a letter or digit.

    Collapses ``U.S.`` / ``US`` / ``u s`` onto one entry without a separate alias row.
    """
    return re.sub(r"[^a-z0-9]", "", (value or "").casefold())


@lru_cache(maxsize=1)
def _country_index() -> dict[str, str]:
    """``{lookup key: alpha2}`` over ISO2, ISO3, English name and every alias."""
    index: dict[str, str] = {}
    for code, entry in _geo()["countries"].items():
        for token in (code, entry["alpha3"], entry["name"], *entry.get("aliases", ())):
            index.setdefault(_key(token), code)
    return index


@lru_cache(maxsize=1)
def _subdivision_index() -> dict[str, str]:
    """``{lookup key: alpha2 of the parent country}`` for US/CA/AU codes and full names.

    US is inserted first, so the handful of code collisions (``WA``: Washington vs
    Western Australia, ``NT``: Northwest Territories vs Northern Territory) resolve to
    the US reading, which is what ATS boards overwhelmingly mean.
    """
    index: dict[str, str] = {}
    for country, subs in _geo()["subdivisions"].items():
        for code, name in subs.items():
            index.setdefault(_key(code), country)
            index.setdefault(_key(name), country)
    return index


def country_by_token(token: str | None) -> tuple[str, str] | None:
    """``"Deutschland"`` / ``"DEU"`` / ``"de"`` -> ``("DE", "Germany")``; else ``None``."""
    code = _country_index().get(_key(token))
    return (code, _geo()["countries"][code]["name"]) if code else None


def country_name(code: str | None) -> str | None:
    """ISO2 -> English name, so a structured path never has one without the other."""
    entry = _geo()["countries"].get((code or "").upper())
    return entry["name"] if entry else None


def country_currency(code: str | None) -> str | None:
    """ISO2 -> ISO 4217, used only by §4.5.3's Greenhouse multi-range tie-break."""
    entry = _geo()["countries"].get((code or "").upper())
    return entry.get("currency") if entry else None


def subdivision_country(token: str | None) -> str | None:
    """``"CA"`` -> ``"US"`` (California), ``"NSW"`` -> ``"AU"``; ``None`` when unknown."""
    return _subdivision_index().get(_key(token))


# --- text cleaning --------------------------------------------------------------------

#: §4.5.1 step 3, in order. Each strips a workplace marker and remembers it for §4.5.2.
_MARKER_PATTERNS = (
    re.compile(r"^\s*(remote|hybrid|on-?site|in-?office)\s*[-–—:,]\s*", re.I),
    re.compile(r"\s*[(\[]\s*(remote|hybrid|on-?site)\s*[)\]]\s*$", re.I),
    re.compile(r"\s*[-–—]\s*(remote|hybrid)\s*$", re.I),
)

#: §4.5.1 step 6 — text that names no place we can resolve: macro-regions, the workplace
#: markers, and the stopwords that glue them together ("Anywhere in the World"). Country
#: names and ISO codes are deliberately absent, so ``"Remote - US"`` still resolves to US.
_REGION_ONLY_WORDS = frozenset(
    """
    emea apac apj anz amer amers americas latam noram nordics benelux dach anywhere
    worldwide global international everywhere world distributed
    remote hybrid onsite site office
    in the of on at and or
    """.split()
)

#: Multi-location separators (§4.5.1 step 2). ``,`` is deliberately absent: it is the
#: within-one-location separator that step 4 consumes.
_SPLIT_RE = re.compile(r";|\s\|\s|\s/\s|\s+or\s+|\s+and\s+", re.I)


def clean_text(value: str | None) -> str:
    """§4.5.1 step 7: normalize NBSPs, collapse whitespace. Casing is never rewritten."""
    if not value:
        return ""
    return " ".join(value.replace(" ", " ").replace(" ", " ").split())


def strip_workplace_markers(value: str | None) -> tuple[str, str | None]:
    """§4.5.1 step 3 -> ``(text without markers, the marker that was stripped)``.

    The marker is returned rather than discarded because §4.5.2 rank 2 is defined as
    "whatever step 3 stripped". Returning it keeps that one fact in one place.
    """
    text = clean_text(value)
    marker: str | None = None
    for pattern in _MARKER_PATTERNS:
        match = pattern.search(text)
        if match:
            marker = marker or match.group(1).lower()
            text = clean_text(pattern.sub(" ", text))
    return text, marker


def is_region_only(value: str | None) -> bool:
    """§4.5.1 step 6: every word is a macro-region or a workplace marker."""
    words = [w for w in re.split(r"[^A-Za-z]+", value or "") if w]
    return bool(words) and all(w.casefold() in _REGION_ONLY_WORDS for w in words)


def split_location_text(value: str | None) -> list[str]:
    """§4.5.1 step 2. Splits only when every resulting part looks like a location.

    ``"Tokyo, Japan; Singapore"`` -> two parts (the second is a known country).
    ``"Research and Development"`` -> one part (neither half qualifies).
    """
    text = clean_text(value)
    if not text:
        return []
    parts = [clean_text(p) for p in _SPLIT_RE.split(text)]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        return [text]
    ok = all(
        "," in p or country_by_token(strip_workplace_markers(p)[0]) or is_region_only(p)
        for p in parts
    )
    return parts if ok else [text]


# --- parsing --------------------------------------------------------------------------

_STRUCTURED_KEYS = {
    "city": ("city", "addressLocality", "locality", "town"),
    "region": ("region", "state", "addressRegion", "province"),
    "country": ("country", "addressCountry", "countryName"),
    "countryCode": ("countryCode", "country_code", "countryISO", "iso"),
}


def _structured_value(structured: dict[str, Any], field: str) -> str | None:
    for key in _STRUCTURED_KEYS[field]:
        value = structured.get(key)
        if isinstance(value, dict):  # Breezy: ``state: {"name": …}``
            value = value.get("name") or value.get("label") or value.get("code")
        if isinstance(value, str) and value.strip():
            return clean_text(value)
    return None


def parse_location(raw: str | None, structured: dict[str, Any] | None = None) -> Location:
    """Parse one location string (§4.5.1).

    ``structured`` wins outright when it carries anything usable (step 1) and accepts the
    Ashby ``postalAddress``, Recruitee ``locations[]`` and Breezy ``location`` shapes as
    they come off the wire, so adapters do not each need a translation table.
    """
    raw_text = clean_text(raw) or None
    if structured:
        loc = _from_structured(structured)
        if loc.city or loc.region or loc.countryCode:
            loc.raw = raw_text or ", ".join(p for p in (loc.city, loc.region, loc.country) if p)
            return loc

    if not raw_text:
        return Location()

    # Step 2: on multi-location text the first part is the primary one. Done here as well
    # as in :func:`parse_locations` so a lone ``parse_location`` call cannot end up with
    # ``city="Tokyo, Japan; Singapore"``.
    parts = split_location_text(raw_text)
    if len(parts) > 1:
        return parse_location(parts[0])

    text, _marker = strip_workplace_markers(raw_text)
    if not text or is_region_only(text):
        # Step 6: macro-regions resolve to nothing, but the raw string is preserved and
        # §4.5.2 rank 2 still reads "remote" out of it.
        return Location(raw=raw_text)

    return _from_text(text, raw_text)


def _from_structured(structured: dict[str, Any]) -> Location:
    city = _structured_value(structured, "city")
    region = _structured_value(structured, "region")
    country = _structured_value(structured, "country")
    code = _structured_value(structured, "countryCode")

    # §4.5.1 step 1: upper-case the code for every provider, and never leave one of
    # (country, countryCode) populated while the other is null.
    code = (code or "").upper() or None
    if code and not country_name(code):
        match = country_by_token(code)  # an ISO3 or a name arrived in the code field
        code = match[0] if match else code
    if not code and country:
        match = country_by_token(country)
        if match:
            code = match[0]
    if code:
        country = country_name(code) or country
    return Location(city=city, region=region, country=country, countryCode=code)


def _from_text(text: str, raw_text: str) -> Location:
    """§4.5.1 step 4: comma-split, consumed right to left."""
    parts = [p for p in (clean_text(p) for p in text.split(",")) if p]
    if not parts:
        return Location(raw=raw_text)

    code: str | None = None
    country: str | None = None
    region: str | None = None
    index = len(parts) - 1

    # ponytail: a token that is both a country and a US/CA/AU subdivision (``CA``,
    # ``Georgia``, ``IN``) is read as the subdivision whenever a city precedes it,
    # because "City, ST" is the dominant ATS convention — "Tbilisi, Georgia" is the
    # known cost. Ceiling: fix by shipping a city gazetteer, which §4.5.1 step 9 rules out.
    last_is_subdivision = index >= 1 and subdivision_country(parts[index]) is not None
    if not last_is_subdivision:
        match = country_by_token(parts[index])
        if match:
            code, country = match
            index -= 1

    # Country-first order. Agile Robots writes its Personio `office` as
    # "Germany, Munich (HQ)" on 64 of 64 jobs; right-to-left alone found no country and
    # then swept the word "Germany" into `city`, so a board that names its country
    # outright shipped `countryCode: null` and a city of "Germany, Munich (HQ)". Only
    # consulted once the right-to-left walk has failed, so "Atlanta, Georgia" is still
    # read as the US state it almost always means.
    if code is None and index >= 1:
        match = country_by_token(parts[0])
        if match:
            code, country = match
            parts = parts[1:]
            index -= 1

    if index >= 0:
        parent = subdivision_country(parts[index])
        if parent and (code is None or parent == code):
            region = parts[index]
            code = code or parent
            index -= 1

    if code:
        country = country_name(code) or country
    city = ", ".join(parts[: index + 1]) or None
    return Location(raw=raw_text, city=city, region=region, country=country, countryCode=code)


def parse_locations(
    raw: str | None = None,
    extra: list[str] | None = None,
    structured: dict[str, Any] | list[dict[str, Any]] | None = None,
) -> tuple[Location, list[Location]]:
    """``(primary, locations[])`` for one job (§4.5.1 steps 2 and 8).

    ``raw`` is the provider's primary location string, ``extra`` any further strings
    (Lever ``categories.allLocations``, Ashby ``secondaryLocations``, Rippling's merged
    ``workLocation`` labels). ``structured`` is one dict (applied to the primary) or one
    per location. ``locations[]`` comes back deduped and sorted by
    ``(countryCode, region, city, raw)`` so a provider reshuffle cannot flip ``changeHash``.
    """
    struct_list = structured if isinstance(structured, list) else None
    primary_struct = structured if isinstance(structured, dict) else None

    texts = split_location_text(raw)
    for value in extra or []:
        texts.extend(split_location_text(value))

    parsed: list[Location] = []
    for position, text in enumerate(texts):
        struct = primary_struct if position == 0 else None
        if struct_list is not None and position < len(struct_list):
            struct = struct_list[position]
        parsed.append(parse_location(text, struct))

    if not parsed:
        if struct_list:
            parsed = [parse_location(None, s) for s in struct_list]
        elif primary_struct:
            parsed = [parse_location(None, primary_struct)]
        else:
            return Location(), []

    primary = parsed[0]
    seen: dict[tuple[Any, ...], Location] = {}
    for loc in parsed:
        seen.setdefault((loc.raw, loc.city, loc.region, loc.countryCode), loc)
    ordered = sorted(seen.values(), key=lambda loc: loc.sort_key)
    return _resolved_primary(primary, ordered), ordered


def _resolved_primary(primary: Location, ordered: list[Location]) -> Location:
    """Fill the flat location fields from ``locations[]`` when the primary names no place.

    Cloudflare's Greenhouse board puts the *workplace type* in ``location.name`` —
    ``"Hybrid"`` on 207 of 310 jobs, ``"In-Office"`` on 44, ``"Distributed"`` on 44 — and
    the real place only in ``offices[].location``. Step 6 correctly resolves ``"Hybrid"``
    to nothing, which used to leave ``city``/``region``/``country``/``countryCode`` null
    on 95% of the board while the parsed office sat one field away in ``locations[]``.

    ``raw`` is deliberately left as the provider's own primary string, so ``locationRaw``
    keeps the §4.2 "untouched" contract, ``contentKey`` does not move, and §4.5.2 rank 2
    still reads ``"Hybrid"`` out of it.

    ponytail: a multi-office posting gets the first office after the §4.5.1 step 8 sort,
    which is a pick, not a fact. Ceiling — the full list is still in ``locations[]``;
    revisit only if buyers ask for a ranked primary.
    """
    if primary.city or primary.region or primary.country or primary.countryCode:
        return primary
    for loc in ordered:
        if loc.city or loc.countryCode:
            return Location(primary.raw, loc.city, loc.region, loc.country, loc.countryCode)
    return primary
