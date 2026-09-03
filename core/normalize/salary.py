"""Salary parsing (SPEC v2 §4.5.3): structured first, regex second, null third.

Step 1 reads the provider's own compensation object (``salarySource="ats"``). Step 2 falls
back to a regex over the provider's free-text salary field and then the first 4,000
characters of the ad body (``salarySource="parsed"``), behind five rejection gates and a
poison-word guard. Step 3 emits nulls — a wrong salary is worse than no salary (R13).

Redaction runs before this module (§4.5.3), so a phone number cannot be read as pay.
No LLM, ever.
"""

from __future__ import annotations

import re
from typing import Any

from core.models import Location, Salary
from core.normalize.location import country_currency

#: Step 2 never looks past this many characters of description text.
DESCRIPTION_SCAN_CHARS = 4000
#: Step 2 evaluates at most this many candidate matches per job (§4.5.3).
MAX_CANDIDATES = 3

_ASHBY_INTERVAL = {"YEAR": "year", "MONTH": "month", "WEEK": "week", "DAY": "day", "HOUR": "hour"}


def ashby_interval(raw: str | None) -> str | None:
    """Ashby sends ``"<n> <UNIT>"`` — ``"1 YEAR"``, ``"1 HOUR"`` (§4.5.3, verbatim).

    ``PER_YEAR`` appears nowhere in the live data. ``"2 WEEKS"`` is not a period we
    normalize and yields ``None``, with min/max/currency still preserved by the caller.
    """
    if not raw:
        return None
    parts = raw.strip().upper().split()
    unit = parts[-1].rstrip("S")  # tolerate "2 WEEKS"
    if len(parts) == 2 and parts[0] != "1":
        return None  # "2 WEEKS" is not a period we normalize; emit null
    return _ASHBY_INTERVAL.get(unit)


def normalize_interval(value: object) -> str | None:
    """Any provider's interval wording -> ``year|month|week|day|hour``, else ``None``.

    Covers Lever's ``per-year-salary``/``per-hour-wage``, Recruitee's bare ``year``, and
    the interval words inside a Greenhouse range ``title``/``blurb``.
    """
    if not isinstance(value, str):
        return None
    text = value.casefold()
    for needle, resolved in (
        ("hour", "hour"),
        ("hourly", "hour"),
        ("year", "year"),
        ("annual", "year"),
        ("annum", "year"),
        ("month", "month"),
        ("week", "week"),
        ("day", "day"),
        ("daily", "day"),
    ):
        if needle in text:
            return resolved
    return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d.\-]", "", value)
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _currency(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip().upper()[:3]
    return None


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


# --- step 1: structured -----------------------------------------------------------------


def _ashby(job: dict[str, Any]) -> Salary | None:
    comp = job.get("compensation")
    if not isinstance(comp, dict):
        return None
    summary = _text(comp.get("compensationTierSummary")) or _text(
        comp.get("scrapeableCompensationSalarySummary")
    )
    components = [
        c
        for c in comp.get("summaryComponents") or []
        if isinstance(c, dict) and c.get("compensationType") == "Salary"
    ]
    if not components:
        return Salary(raw=summary) if summary else None

    # Widest min…max across the Salary components (§4.5.3) — but only *within* one
    # currency+interval. Widening across all of them while reading `currency`/`interval`
    # off `components[0]` produced, on a live posting carrying both an hourly and an
    # annual band, `min=25.0 max=200000.0 interval='hour'` labelled `salarySource: "ats"`
    # (V1 H5). Spanning a 1:8,000 range is not a wider answer, it is a wrong one.
    groups: dict[tuple[str | None, str | None], list[dict[str, Any]]] = {}
    for component in components:
        key = (
            _currency(component.get("currencyCode")),
            ashby_interval(_text(component.get("interval"))),
        )
        groups.setdefault(key, []).append(component)
    (currency, interval), chosen = max(groups.items(), key=lambda item: len(item[1]))

    lows = [v for v in (_number(c.get("minValue")) for c in chosen) if v is not None]
    highs = [v for v in (_number(c.get("maxValue")) for c in chosen) if v is not None]
    return Salary(
        min=min(lows) if lows else None,
        max=max(highs) if highs else None,
        currency=currency,
        interval=interval,
        source="ats",
        raw=summary,
    )


def _lever(job: dict[str, Any]) -> Salary | None:
    summary = _text(job.get("salaryDescription"))
    band = job.get("salaryRange")
    low, high = _number((band or {}).get("min")), _number((band or {}).get("max"))
    # A Lever employer can switch the pay field on and leave it at zero, so `{"min": 0,
    # "max": 0}` arrives looking like a declared range (seeker-os#35). That is pay *absent*,
    # not a job paying nothing, and publishing it as `source="ats"` would assert a fact the
    # employer never stated. A zero *min* beside a real max is a genuine open-ended low end
    # and is kept.
    if not isinstance(band, dict) or not band or not (low or high):
        return Salary(raw=summary) if summary else None
    return Salary(
        min=low,
        max=high,
        currency=_currency(band.get("currency")),
        # `interval` here is typed by the employer, not chosen from a list, so it gets the
        # same magnitude test as a Greenhouse range label rather than being believed.
        interval=_believable_interval(
            normalize_interval(band.get("interval")), high if high is not None else low
        ),
        source="ats",
        raw=summary,
    )


#: §4.5.3's own year/hour rejection bounds, reused to test whether a Greenhouse range's
#: *label* is believable. Live evidence that it often is not: Verkada publishes
#: ``{"title": "Estimated Hourly Pay Range", "min_cents": 20000000}`` (= $200,000 "an
#: hour") and a $1.00 placeholder beside a real annual range, and Rocket Lab publishes
#: hourly technician rates (``min_cents: 1885``) under a label with no interval word at
#: all — where the old ``or "year"`` default shipped "$18.85 per year".
_YEAR_FLOOR = 1_000
_HOUR_CEILING = 2_000
#: The smallest amount each period could honestly pay, derived from the $2/hour floor the
#: unlabelled branch already uses times the hours in that period — not a separate guess.
#: It exists because a *provider* label can be wrong in the small direction: a live gopuff
#: advert (seeker-os#35) carries `bi-week-salary` on a 22.4-26 band that is plainly hourly,
#: and $26 a week is below any real wage. Above the floor the label is taken at its word.
_PERIOD_FLOOR = {"hour": 2, "day": 2 * 8, "week": 2 * 40, "month": 2 * 160, "year": _YEAR_FLOOR}


def _believable_interval(said: str | None, amount: float | None) -> str | None:
    """The interval an amount really has, or ``None`` when label and magnitude disagree.

    §4.5.3 step 3: a null beats a wrong answer. Shared by the Greenhouse range reader and
    the Lever band reader so both use one set of bounds (R13).
    """
    if amount is None:
        return said
    if said == "hour":
        return "hour" if _PERIOD_FLOOR["hour"] <= amount <= _HOUR_CEILING else None
    if said:
        floor = _PERIOD_FLOOR.get(said)
        return said if floor is None or amount >= floor else None
    return "year" if amount >= _YEAR_FLOOR else "hour" if amount >= _PERIOD_FLOOR["hour"] else None


def _range_amount(band: dict[str, Any]) -> float | None:
    """A Greenhouse range's magnitude in currency units (``*_cents`` / 100)."""
    cents = _number(band.get("max_cents"))
    if cents is None:
        cents = _number(band.get("min_cents"))
    return cents / 100 if cents is not None else None


def _range_interval(band: dict[str, Any]) -> str | None:
    """The interval a Greenhouse range really has, or ``None`` when we cannot tell.

    The label is believed only while the magnitude agrees with it; where they contradict
    each other neither is evidence, and §4.5.3 step 3 (nulls) beats a wrong answer (R13).
    An unlabelled range is read by magnitude alone, exactly as step 2 already reads an
    unlabelled regex match.

    Only ``title`` is read, never ``blurb``: the blurb is a benefits paragraph, and Rocket
    Lab's says "3 weeks paid vacation and 5 days sick leave **per year**" on hourly
    technician bands. ``_pick_greenhouse_range`` still matches locations across both,
    where the blurb genuinely carries the locale.

    ponytail: the two thresholds are USD-shaped, like §4.5.3's own gates. Ceiling — a
    board paying ¥2,500/hour would have its correct "hour" label dropped to null; fix by
    scaling the bounds per currency if a live board ever shows one.
    """
    return _believable_interval(normalize_interval(_text(band.get("title"))), _range_amount(band))


def _greenhouse(job: dict[str, Any], location: Location | None) -> Salary | None:
    ranges = [r for r in job.get("pay_input_ranges") or [] if isinstance(r, dict)]
    if not ranges:
        return None
    chosen, raw = _pick_greenhouse_range(ranges, location)
    # Several bands come back only when they could not be told apart; then the honest
    # answer is their span, exactly as `_ashby` spans its Salary components.
    lows = [v for v in (_number(band.get("min_cents")) for band in chosen) if v is not None]
    highs = [v for v in (_number(band.get("max_cents")) for band in chosen) if v is not None]
    intervals = {_range_interval(band) for band in chosen}
    return Salary(
        min=min(lows) / 100 if lows else None,
        max=max(highs) / 100 if highs else None,
        currency=_currency(chosen[0].get("currency_type")),
        interval=intervals.pop() if len(intervals) == 1 else None,
        source="ats",
        raw=raw,
    )


_TOTAL_PAY_LABELS = ("on-target", "on target", "total target", "total cash", "total compensation")
_OTE = re.compile(r"\bote\b")


def _is_total_pay(band: dict[str, Any]) -> bool:
    """A band measuring base + variable pay, which is not the base band beside it.

    DoorDash publishes both on one advert: "The national base pay range for this
    position…" at 1,937-3,250 next to "The total on-target earnings (base +
    commissions)" at 3,400-5,000, on 60 of its 462 postings measured 2026-09-03.
    They tie on currency and interval and neither names a place, so every earlier
    tie-break passed them through and the span put the commission number in
    ``salaryMax`` — Account Manager, CPG went out as 85,680-168,500 when base tops
    out at 126,000, a 34% overstatement asserted as ``salarySource: "ats"``.

    ``_ashby`` never had this bug because Ashby types its components and we keep
    only ``compensationType == "Salary"``. Greenhouse has no type field, so the
    employer's own label is the only signal there is.

    Reported by @GregoryBolshakov on tonyperkins/seeker-os#35.
    """
    label = (_text(band.get("title")) or "").casefold()
    return any(term in label for term in _TOTAL_PAY_LABELS) or bool(_OTE.search(label))


def _pick_greenhouse_range(
    ranges: list[dict[str, Any]], location: Location | None
) -> tuple[list[dict[str, Any]], str | None]:
    """§4.5.3 multi-range selection. Taking ``[0]`` attaches a US salary to a Dublin job.

    Returns ``(bands, salaryRaw)``. **More than one band means the ranges could not be
    told apart** and the caller must span them rather than pick one. ``salaryRaw`` names
    the titles involved whenever the answer was not a clean tie-break, so the buyer can
    see what they got.
    """
    if len(ranges) == 1:
        return ranges, None

    # A range whose label contradicts its own magnitude is not a candidate while a
    # coherent one exists: Verkada publishes a $1.00 "Estimated Hourly Pay Range" beside
    # the real $225k-$265k annual range, and `ranges[0]` used to win it outright.
    ranges = [band for band in ranges if _range_interval(band) is not None] or ranges
    if len(ranges) == 1:
        return ranges, None

    # A base band and a total-pay band are not two guesses at one number, so spanning
    # them is not a wider answer, it is a wrong one. Keep base pay while any survives.
    ranges = [band for band in ranges if not _is_total_pay(band)] or ranges
    if len(ranges) == 1:
        return ranges, None

    places = (location.country, location.region, location.city) if location else ()
    wanted = [w.casefold() for w in places if w]
    for candidate in ranges:
        label = " ".join(
            filter(None, (_text(candidate.get("title")), _text(candidate.get("blurb"))))
        ).casefold()
        if label and any(word in label for word in wanted):
            return [candidate], None

    # The currency tie-break *narrows* the candidates; it never picks one. Taking the
    # first currency match was the same coin-flip as taking `ranges[0]` whenever a board
    # prices one posting in one currency several times over.
    currency = country_currency(location.countryCode) if location else None
    if currency:
        ranges = [b for b in ranges if _currency(b.get("currency_type")) == currency] or ranges
    if len(ranges) == 1:
        return ranges, None

    # Indistinguishable bands: Databricks publishes "Zone 1..4 Pay Range" for one US
    # posting — same currency, same interval, no place anywhere in the label — and
    # `ranges[0]` published Zone 1, the *top* band, as the salary on 137 of its 139
    # multi-range jobs, overstating the floor by 25% ($146,600 against $117,300).
    titles = [t for t in (_text(band.get("title")) for band in ranges) if t]
    if len({(_currency(b.get("currency_type")), _range_interval(b)) for b in ranges}) == 1:
        return ranges, " / ".join(titles) or None
    return [ranges[0]], _text(ranges[0].get("title"))


def _recruitee(job: dict[str, Any]) -> Salary | None:
    band = job.get("salary")
    if not isinstance(band, dict) or not band:
        return None
    return Salary(
        min=_number(band.get("min")),
        max=_number(band.get("max")),
        currency=_currency(band.get("currency")),
        interval=normalize_interval(band.get("period")),
        source="ats",
        raw=None,
    )


def _rippling(job: dict[str, Any]) -> Salary | None:
    details = [d for d in job.get("payRangeDetails") or [] if isinstance(d, dict)]
    if not details:
        return None
    band = details[0]
    return Salary(
        min=_number(band.get("min") if "min" in band else band.get("minValue")),
        max=_number(band.get("max") if "max" in band else band.get("maxValue")),
        currency=_currency(band.get("currency") or band.get("currencyCode")),
        interval=normalize_interval(band.get("frequency") or band.get("interval")),
        source="ats",
        raw=_text(band.get("payRangeSummary")),
    )


def structured_salary(job: dict[str, Any], location: Location | None = None) -> Salary | None:
    """Step 1 (§4.5.3). Dispatch is on which compensation object is present, so the
    adapters stay free of salary logic and a provider that adds one is picked up here."""
    for extract in (_ashby, _lever, _recruitee, _rippling):
        found = extract(job)
        if found is not None:
            return _checked(found)
    found = _greenhouse(job, location)
    return _checked(found) if found is not None else None


def _checked(found: Salary) -> Salary:
    """§4.5.3 step 3: a compensation object with no numbers in it is a *failed* step 1.

    Claiming ``salarySource: "ats"`` for it both lies about provenance and short-circuits
    the regex fallback in :func:`parse_salary` — Lever ``salaryRange: {"currency": "USD"}``,
    a Greenhouse range with no ``min_cents`` and a Rippling band with neither bound all
    did exactly that (V1 H4). Gated once here, for every provider.

    An *implausible* band fails step 1 the same way (V1 H5): §4.5.3's ratio and magnitude
    gates used to run on the regex path only, so the structured path — the one the output
    labels as the ATS's own answer — was the ungated one on all six providers. Dropping
    ``source`` here is not a discard: :func:`parse_salary` already treats a non-``"ats"``
    result as a failed step 1 and falls through to the regex, which is the degradation
    path this function was written for.
    """
    if found.source == "ats" and found.min is None and found.max is None:
        found.source = None
    elif found.source == "ats" and not _plausible(found.min, found.max, found.interval):
        found.source = None
    return found


def _plausible(low: float | None, high: float | None, interval: str | None) -> bool:
    """§4.5.3's two *currency-agnostic* gates, shared by step 1 and step 2.

    Only these two are shared. §4.5.3's absolute year/hour bounds are deliberately left on
    the step-2 path alone: Ashby publishes a real ``COP 248M – COP 310M`` per year (about
    $60k USD), so any absolute magnitude gate must be currency-aware or it deletes honest
    data — and an ATS publishing its own structured number has earned more benefit of the
    doubt than a regex over prose. A band that contradicts *itself* needs no currency
    table to be recognised, which is what both paths can safely reject.
    """
    del interval  # kept in the signature for the caller's readability at both call sites
    if low is None or high is None:
        return True  # a one-sided band says nothing about its own plausibility
    return not (low > high or (low > 0 and high / low > 20))


# --- step 2: regex fallback ---------------------------------------------------------------

_ISO_CODES = "USD|EUR|GBP|CAD|AUD|CHF|SEK|NOK|DKK|PLN|INR|SGD|NZD|JPY"
_NUM = r"\d{1,3}(?:[,  ]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "¥": "JPY"}
_DOLLAR_PREFIX = {"CA": "CAD", "C": "CAD", "A": "AUD", "S": "SGD", "NZ": "NZD"}


def _cur_group(n: int) -> str:
    """Optional leading currency: a symbol (with its ``CA``/``A``/``S``/``NZ`` prefix) or
    an explicit ISO code."""
    return rf"(?:(?:(?P<pfx{n}>CA|C|A|S|NZ)?(?P<sym{n}>[$€£₹¥])|\b(?P<iso{n}>{_ISO_CODES})\b)\s*)?"


_RANGE = re.compile(
    rf"{_cur_group(1)}(?P<num1>{_NUM})\s*(?P<k1>[kK])?\s*"
    r"(?:-|–|—|\bto\b|\band\b|\buntil\b)\s*"
    rf"{_cur_group(2)}(?P<num2>{_NUM})\s*(?P<k2>[kK])?"
    rf"(?:\s*\b(?P<iso3>{_ISO_CODES})\b)?"
)

_POISON = re.compile(
    r"(?i)(401|equity|options|bonus|revenue|ARR|MRR|funding|raised|valuation|budget"
    r"|Series [A-Z]|market cap|savings)"
)
_DATE_RANGE = re.compile(r"(?P<a>\d{4})\s*[-–]\s*(?P<b>\d{4})")

#: A unit noun immediately after the range makes it a duration, not pay: Palantir's
#: "5 to 10 hours per week" and Stripe's "6–24 months" both parsed as salaries before
#: this gate (live contract run, §10.2). Real pay never reads "300 - 450 hours per day".
_UNIT_SUFFIX = re.compile(r"(?i)^\+?\s*(?:hours?|hrs?|days?|weeks?|months?|years?|yrs?)\b")

#: An ISO code trailing the interval wording — Lever palantir publishes
#: "110,000 - 200,000/year SGD", where the code sits past `iso3`'s reach.
_TRAILING_ISO = re.compile(rf"(?i)^[\s/\-]*(?:per\s+|/\s?)?[a-z.]{{0,8}}\s*\b({_ISO_CODES})\b")
_TRAILING_ISO_CHARS = 16

#: Interval wording within ±40 characters of the match, in §4.5.3 order.
_INTERVAL_WINDOW = (
    (re.compile(r"(?i)per\s+year|/\s?yr|annual(?:ly)?|yearly|per\s+annum|p\.a\."), "year"),
    (re.compile(r"(?i)per\s+hour|/\s?hr|hourly|/hour"), "hour"),
    (re.compile(r"(?i)per\s+month|/\s?mo\b|monthly"), "month"),
    (re.compile(r"(?i)per\s+week|weekly"), "week"),
    (re.compile(r"(?i)per\s+day|daily|/day"), "day"),
)
_INTERVAL_WINDOW_CHARS = 40


def _match_currency(match: re.Match[str], text: str = "") -> str | None:
    """Explicit ISO code wins; otherwise the symbol, disambiguated by its prefix.

    The trailing-ISO lookahead is checked last, not first: a code sixteen characters
    away is weaker evidence than a symbol sitting on the number itself.
    """
    for group in ("iso1", "iso2", "iso3"):
        if match.group(group):
            return match.group(group).upper()
    for index in ("1", "2"):
        symbol = match.group("sym" + index)
        if symbol:
            prefix = (match.group("pfx" + index) or "").upper()
            if symbol == "$" and prefix in _DOLLAR_PREFIX:
                return _DOLLAR_PREFIX[prefix]
            return _SYMBOLS[symbol]
    trailing = _TRAILING_ISO.match(text[match.end() : match.end() + _TRAILING_ISO_CHARS])
    return trailing.group(1).upper() if trailing else None


def _match_amount(raw: str, suffix: str | None) -> float | None:
    value = _number(raw.replace(",", "").replace(" ", "").replace(" ", ""))
    if value is None:
        return None
    return value * 1000 if suffix else value


def _window_interval(text: str, match: re.Match[str]) -> str | None:
    start = max(0, match.start() - _INTERVAL_WINDOW_CHARS)
    window = text[start : match.end() + _INTERVAL_WINDOW_CHARS]
    for pattern, resolved in _INTERVAL_WINDOW:
        if pattern.search(window):
            return resolved
    return None


def _rejected(text: str, match: re.Match[str], low: float, high: float, interval: str) -> bool:
    """The five rejection gates plus the poison-word guard (§4.5.3).

    The ratio gate is :func:`_plausible`, shared with step 1 (V1 H5); the absolute
    year/hour bounds stay here, because they are USD-shaped and step 1 has the provider's
    own currency-tagged word for it.
    """
    if not _plausible(low, high, interval):
        return True
    if interval == "year" and (high > 5_000_000 or low < 1_000):
        return True
    if interval == "hour" and (high > 2_000 or low < 2):
        return True
    if _POISON.search(text[max(0, match.start() - 30) : match.start()]):
        return True
    if "%" in text[max(0, match.start() - 2) : match.end() + 2]:
        return True
    date_range = _DATE_RANGE.search(match.group(0))
    if date_range and all(1900 <= int(date_range.group(g)) <= 2100 for g in ("a", "b")):
        return True
    if _UNIT_SUFFIX.match(text[match.end() : match.end() + 12]):
        return True
    # Part of a longer number (a phone number that survived redaction, an id).
    return match.start() > 0 and text[match.start() - 1] in "+0123456789"


def parse_salary_text(text: str | None) -> Salary | None:
    """Step 2 over one blob of text. ``None`` when nothing survives the gates."""
    if not text:
        return None
    evaluated = 0
    for match in _RANGE.finditer(text):
        if evaluated >= MAX_CANDIDATES:
            break
        evaluated += 1
        low = _match_amount(match.group("num1"), match.group("k1"))
        high = _match_amount(match.group("num2"), match.group("k2"))
        if low is None or high is None:
            continue

        currency = _match_currency(match, text)
        interval = _window_interval(text, match)
        if interval is None:
            # §4.5.3: infer only from magnitude, else discard the whole match.
            if match.group("k1") or match.group("k2") or high >= 1000:
                interval = "year"
            elif 15 <= high <= 500:
                interval = "hour"
            else:
                continue
            # Deviation from §4.5.3's five gates, on R13's authority ("a wrong salary is
            # worse than no salary"): a bare two-digit range with no currency, no `k` and
            # no interval wording is a head-count, not a wage. Stripe's live board gave
            # "org of 60–100+ Stripes" -> $60–100/hour and "team of 15-20" -> $15–20/hour
            # (§10.2 live run). Magnitude alone is not evidence that a number is money.
            if currency is None and not (match.group("k1") or match.group("k2")):
                continue
        if _rejected(text, match, low, high, interval):
            continue
        return Salary(
            min=low,
            max=high,
            currency=currency,
            interval=interval,
            source="parsed",
            raw=match.group(0).strip(),
        )
    return None


# --- entry point ---------------------------------------------------------------------------


def parse_salary(
    job: dict[str, Any] | None = None,
    description_head: str | None = None,
    location: Location | None = None,
    salary_text: str | None = None,
) -> Salary:
    """``Salary`` for one job (§4.5.3), structured first, regex second, nulls third.

    ``salary_text`` is the provider's free-text pay field (Lever ``salaryDescription``);
    it is searched before the description body. When both steps fail, every numeric field
    is null and ``salarySource`` is null — but ``salaryRaw`` still carries the provider's
    own free text when there was any.
    """
    job = job or {}
    structured = structured_salary(job, location)
    if structured and structured.source == "ats":
        return structured

    fallback_raw = structured.raw if structured else None
    free_text = salary_text or fallback_raw or _text(job.get("salaryDescription"))
    for blob in (free_text, (description_head or "")[:DESCRIPTION_SCAN_CHARS]):
        parsed = parse_salary_text(blob)
        if parsed:
            parsed.raw = fallback_raw or parsed.raw
            return parsed
    return Salary(raw=fallback_raw)
