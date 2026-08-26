"""Personio adapter (SPEC v2 §5.8).

One request per company::

    GET https://{slug}.jobs.personio.de/xml   ->  <workzag-jobs><position>…

**No ``?language=`` parameter.** Personio serves the requested language and does *not*
fall back to the ad's own: on ``1komma5grad`` (322 positions) ``?language=en`` returns 35
positions with a body and 287 with ``<jobDescriptions></jobDescriptions>``, while the
bare URL returns 321. Since ``descriptionText`` also feeds §4.5.2 rank 5 and §4.5.3
step 2, asking for English on a German board silently emptied the body, the remote flag
and the parsed salary for 89% of the board's jobs.

No pagination, no detail call — the description sections are inline as CDATA. An unknown
board answers **307** (§5.12 maps 3xx to `not_found`, and `core.http.Client` never follows
a redirect, so a missing board can never come back as a 200 marketing page); the `.com`
host is tried before giving up because both spellings are live slug forms (§5.8).

The feed is third-party XML, so it is parsed with `defusedxml` and never with stdlib
`xml.etree` — billion-laughs and external-entity defence (§5.8, §9.3).

`seniority` and `yearsOfExperience` are **provider-sourced passthrough**, copied verbatim
from the controlled vocabulary Personio publishes. Nothing here is inferred (§1.2, T-M7).
"""

from __future__ import annotations

from html import escape
from typing import Any

import httpx
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import ParseError as XMLParseError
from defusedxml.ElementTree import fromstring

from core.http import Client, NotFound, ParseError
from core.models import JobRecord, ProviderSpec, Ref
from core.normalize.record import build_job_record

__all__ = ["SPEC", "fetch", "job_url", "list_url", "parse_feed", "to_record"]

#: Undocumented rate limit -> the 2 rps default. Unlike Greenhouse/Lever/Ashby/Rippling,
#: every Personio company is its own host, so the cap is genuinely per company (§5.12).
SPEC = ProviderSpec(name="personio", host_rate_limit=2.0, needs_detail_call=False)

ROOT_TAG = "workzag-jobs"
#: `.de` first, `.com` on failure (§5.8 "Slug forms").
HOSTS: tuple[str, ...] = ("de", "com")


def list_url(slug: str, host: str = "de") -> str:
    return f"https://{slug}.jobs.personio.{host}/xml"


def job_url(slug: str | None, source_id: str | None, host: str = "de") -> str | None:
    """`https://{slug}.jobs.personio.de/job/{id}`, or ``None`` when either half is missing.

    §5.8 marks the pattern unverified and demands a null over a broken link; §10.2 asserts
    it live. The host is the one that answered, so a board reached on `.com` is not handed
    a `.de` link.
    """
    if not slug or not source_id:
        return None
    return f"https://{slug}.jobs.personio.{host}/job/{source_id}"


def _text(value: str | None) -> str | None:
    return value.strip() or None if value else None


def _position(element: Any) -> dict[str, Any]:
    """One `<position>` as a plain dict — the shape `to_record` and `raw` both consume."""
    data: dict[str, Any] = {}
    for child in element:
        if child.tag == "jobDescriptions":
            data[child.tag] = [
                {"name": _text(d.findtext("name")), "value": _text(d.findtext("value"))}
                for d in child.findall("jobDescription")
            ]
        elif child.tag == "additionalOffices":
            data[child.tag] = [t for t in (_text(o.text) for o in child.findall("office")) if t]
        else:
            data[child.tag] = _text(child.text)
    return data


def parse_feed(payload: str | bytes) -> list[dict[str, Any]]:
    """`<workzag-jobs>` -> one dict per `<position>`.

    Raises :class:`core.http.ParseError` on malformed XML **and** on a well-formed body
    that is not the jobs feed (§5.8 "Non-XML body -> parse_error"). Bytes are preferred
    over text so the XML declaration decides the encoding, not the HTTP header.
    """
    try:
        root = fromstring(payload)
    except (XMLParseError, DefusedXmlException) as exc:
        # DefusedXmlException covers the attack payloads (entities, DTD, external refs);
        # both are a `parse_error` to the caller, never a crash (§5.12).
        raise ParseError(f"malformed Personio XML: {exc}") from exc
    if root.tag != ROOT_TAG:
        raise ParseError(f"not a Personio jobs feed: root <{root.tag}>, expected <{ROOT_TAG}>")
    return [_position(position) for position in root.findall("position")]


def _description_html(sections: Any) -> str | None:
    """`jobDescriptions[]` -> one HTML body, each section under its own `<h3>` (§5.8)."""
    parts: list[str] = []
    for section in sections or []:
        if not isinstance(section, dict):
            continue
        name, value = section.get("name"), section.get("value")
        if name:
            # `quote=False`: the header is element text, not an attribute, so an
            # apostrophe stays an apostrophe while `&` and `<` are still escaped.
            parts.append(f"<h3>{escape(name, quote=False)}</h3>")
        if value:
            parts.append(value)
    return "\n".join(parts) or None


def to_record(
    raw: dict[str, Any],
    ref: Ref,
    options: dict[str, Any] | None = None,
    host: str = "de",
) -> JobRecord:
    """One `<position>` dict -> a normalized :class:`JobRecord` (§5.8 mapping table).

    Every read is a `.get()`, so a position that ships empty or absent elements produces
    nulls rather than an exception (§10.1 `test_adapters_empty_objects`).
    """
    raw = raw or {}
    source_id = raw.get("id")
    extracted = {
        "job": raw,
        "sourceId": source_id,
        "title": raw.get("name"),
        # `subcompany` is the legal entity behind the board; the slug is the only fallback.
        "company": raw.get("subcompany") or ref.slug,
        "department": raw.get("department") or raw.get("recruitingCategory"),
        "locationRaw": raw.get("office"),
        "locations": raw.get("additionalOffices") or [],
        "employmentType": raw.get("employmentType"),
        # `schedule` refines permanent -> part_time for working students (§4.5.4).
        "schedule": raw.get("schedule"),
        "seniority": raw.get("seniority"),
        "yearsOfExperience": raw.get("yearsOfExperience"),
        "url": job_url(ref.slug, source_id, host),
        "postedAt": raw.get("createdAt"),
        "postedAtSource": "createdAt",
        "descriptionHtml": _description_html(raw.get("jobDescriptions")),
    }
    return build_job_record(ref, extracted, options)


def _parse_response(response: httpx.Response) -> list[dict[str, Any]]:
    return parse_feed(response.content)


async def fetch(
    ref: Ref,
    client: Client,
    options: dict[str, Any] | None = None,
) -> list[JobRecord]:
    """Every open position on one Personio board.

    `.de` then `.com`; `not_found` only when both answer 307/404. Any other failure
    (`rate_limited`, `http_error`, `timeout`, `parse_error`) propagates from
    :mod:`core.http` with the §5.12 status already attached.
    """
    missing: NotFound | None = None
    for host in HOSTS:
        try:
            positions = await client.get_json(
                list_url(ref.slug, host),
                parse=_parse_response,
            )
        except NotFound as exc:
            missing = exc
            continue
        return [to_record(position, ref, options, host) for position in positions]
    raise missing or NotFound(f"no Personio board for {ref.slug!r}")
