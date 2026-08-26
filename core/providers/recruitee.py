"""Recruitee adapter (SPEC v2 §5.6).

One request per company: ``GET https://{slug}.recruitee.com/api/offers/``. No pagination,
no detail call — ``description`` and ``requirements`` are inline on every offer. **Each
company is its own host**, so the 2 rps cap applies per company and Recruitee never joins
the shared-host queue §7.7 is built around.

Failure semantics (§5.6): 404 ``{"error": "Not Found"}`` -> ``not_found``; a 301 to
``recruitee.com/careers_not_hosted`` -> ``not_found`` with "board is not hosted on
Recruitee" (:class:`core.http.Client` does not follow redirects, so the marketing page is
never mistaken for a board); anything unparseable -> ``parse_error`` after one retry.

⚠ Sunset: the docs give **2027-02-10** as the deadline for sending an
``X-Careers-Sites-Token``. Anonymous calls work today; §17 carries the migration decision.

Three things the live payload does that §5 did not predict — each handled here rather than
in ``core/normalize/`` because each is Recruitee's own dialect, not a normalization rule:

1. ``published_at`` / ``updated_at`` are ``"2026-08-19 10:48:22 UTC"``, which
   ``datetime.fromisoformat`` rejects. :func:`_iso` rewrites the zone word to ``Z``.
2. ``employment_type_code`` is compound — ``fulltime_permanent``, ``fulltime_fixed_term``,
   ``parttime_fixed_term`` (32/32 offers across 3 live boards), never the bare ``fulltime``
   §4.5.4 tabulates. :func:`_employment_code` reads the schedule half; the whole code
   survives in ``employmentTypeRaw``.
3. ``salary`` is present but all-null on every offer of a board that publishes no pay
   (5/5 on nmbrs). That is a *failed* step 1, not an ATS answer, so :func:`_pay_job` hides
   it from the salary parser and lets §4.5.3 step 2 run.
"""

from __future__ import annotations

from typing import Any

from core.http import NotFound, ParseError
from core.models import JobRecord, ProviderSpec, Ref
from core.normalize.record import build_job_record

SPEC = ProviderSpec(
    name="recruitee",
    host_rate_limit=2.0,
    needs_detail_call=False,
    ai_train=True,
    retainable=True,
)

LIST_URL = "https://{slug}.recruitee.com/api/offers/"

#: §5.6 verbatim, for the ``company_summary`` message on a 301 to `careers_not_hosted`.
NOT_HOSTED_MESSAGE = "board is not hosted on Recruitee"

#: The schedule half of a compound ``employment_type_code``. Both map straight onto the
#: §4.5.4 table, so the shared vocabulary stays the single source of truth.
_SCHEDULES = ("fulltime", "parttime")

#: The keys that make a ``locations[]`` entry worth parsing. An entry carrying only an
#: internal ``id`` would otherwise emit an all-null ``Location`` into every row.
_LOCATION_KEYS = ("name", "city", "state", "country", "country_code")


def _iso(value: object) -> object:
    """``"2026-08-19 10:48:22 UTC"`` -> ``"2026-08-19 10:48:22Z"``; anything else as-is.

    ``core.normalize.dates`` handles every ISO shape but not a trailing zone *word*, and
    that is the only form Recruitee emits — so a null ``postedAt`` on every Recruitee job
    is what happens if this one substitution is missing.
    """
    if isinstance(value, str) and value.rstrip().endswith("UTC"):
        return value.rstrip()[:-3].rstrip() + "Z"
    return value


def _employment_code(code: object) -> object:
    """``"fulltime_fixed_term"`` -> ``"fulltime"``; unknown or bare codes pass through.

    A fixed-term contract is how essentially every Dutch full-time job starts, so the
    permanence half is a contract detail rather than an employment type — mapping it to
    ``temporary`` would file five ordinary staff roles on nmbrs under "temporary". The
    full code is preserved in ``employmentTypeRaw`` for buyers who need the distinction.
    """
    if isinstance(code, str):
        head, separator, _tail = code.partition("_")
        if separator and head in _SCHEDULES:
            return head
    return code


def _has_pay(band: object) -> bool:
    """True only when Recruitee's ``salary`` object actually carries a number."""
    if not isinstance(band, dict):
        return False
    return any(band.get(key) not in (None, "") for key in ("min", "max"))


def _pay_job(offer: dict[str, Any]) -> dict[str, Any]:
    """The offer as the normalizers should see it (§4.5.3 step 3).

    An all-null ``salary`` object is still a dict, and a dict is enough for step 1 to
    claim ``salarySource: "ats"`` with no numbers in it — which both lies about provenance
    and suppresses the regex fallback. Blanking it restores the specified order.
    """
    if _has_pay(offer.get("salary")) or "salary" not in offer:
        return offer
    return {**offer, "salary": None}


def _description(offer: dict[str, Any]) -> str | None:
    """§5.6: ``descriptionHtml`` is ``description`` + ``requirements``, both inline HTML."""
    parts = [
        part.strip()
        for part in (offer.get("description"), offer.get("requirements"))
        if isinstance(part, str) and part.strip()
    ]
    return "\n".join(parts) or None


def _locations(offer: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]] | None]:
    """``(location names, structured locations)`` from ``locations[]`` (§4.5.1 step 1).

    The two lists stay index-aligned because ``parse_locations`` pairs them positionally;
    a location with no ``name`` drops the text side entirely rather than shifting it.
    """
    structured = [
        loc
        for loc in offer.get("locations") or []
        if isinstance(loc, dict) and any(loc.get(key) for key in _LOCATION_KEYS)
    ]
    names = [str(loc.get("name") or "").strip() for loc in structured]
    if not all(names):
        names = []
    if not names and not structured:
        primary = offer.get("location")
        return ([primary.strip()] if isinstance(primary, str) and primary.strip() else []), None
    return names, structured or None


def to_record(raw: dict[str, Any], ref: Ref, options: dict[str, Any] | None = None) -> JobRecord:
    """One Recruitee offer -> one :class:`JobRecord` (§5.6 mapping table, §4.5).

    Every read is a ``.get()`` chain: the §10.1 empty-objects test feeds this an offer
    whose every nested object is ``{}`` or absent and requires nulls, not a traceback.
    """
    offer = raw if isinstance(raw, dict) else {}
    names, structured = _locations(offer)
    code = offer.get("employment_type_code")

    record = build_job_record(
        ref,
        {
            "job": _pay_job(offer),
            "sourceId": offer.get("id"),
            "title": offer.get("title"),
            "company": offer.get("company_name"),
            "department": offer.get("department"),
            "locationRaw": names[0] if names else None,
            "locations": names[1:],
            "locationStructured": structured,
            "employmentType": _employment_code(code),
            "url": offer.get("careers_url"),
            "applyUrl": offer.get("careers_apply_url"),
            "postedAt": _iso(offer.get("published_at")),
            "postedAtSource": "published_at",
            "updatedAt": _iso(offer.get("updated_at")),
            "descriptionHtml": _description(offer),
        },
        options,
    )
    if isinstance(code, str) and code.strip():
        record.employmentTypeRaw = code.strip()
    if record.raw is not None:
        record.raw = offer  # `includeRawJson` gets the payload, not the pay-blanked copy
    return record


async def fetch(ref: Ref, client: Any, *, options: dict[str, Any] | None = None) -> list[JobRecord]:
    """Every published offer for one company. One GET, no pagination, no detail call."""
    url = LIST_URL.format(slug=ref.slug)
    try:
        payload = await client.get_json(url)
    except NotFound as exc:
        status = exc.http_status or 0
        if 300 <= status < 400:
            raise NotFound(NOT_HOSTED_MESSAGE, url=url, http_status=status) from exc
        raise

    if not isinstance(payload, dict):
        raise ParseError(f"expected an object, got {type(payload).__name__}: {url}", url=url)
    offers = payload.get("offers")
    if not isinstance(offers, list):
        # `{"error": "Not Found"}` also arrives as a 200 body on some hosts (§5.6).
        if payload.get("error"):
            raise NotFound(f"{payload['error']}: {url}", url=url, http_status=404)
        raise ParseError(f"no offers array: {url}", url=url)
    return [to_record(offer, ref, options) for offer in offers if isinstance(offer, dict)]
