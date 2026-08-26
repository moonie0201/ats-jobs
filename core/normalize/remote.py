"""Remote / workplace-type detection (SPEC v2 §4.5.2).

A six-rank precedence ladder, first hit wins, and every hit records where it came from in
``remoteSource``. Rank 5 reads only the first 1,500 characters of the description and only
two narrow patterns: "we support remote teams" in a culture paragraph at character 3,000
is exactly the false positive this ladder exists to avoid.

``remote`` is ``True``, ``False`` or ``None`` — never a guess.
"""

from __future__ import annotations

import re
from typing import Any

from core.models import Location
from core.normalize.location import is_region_only, strip_workplace_markers

#: Rank 5 never looks past this many characters of description text.
DESCRIPTION_SCAN_CHARS = 1500

_HYBRID = re.compile(r"(?i)\bhybrid\b")
_ONSITE = re.compile(r"(?i)\b(on-?site|in-?office|in person|in-person)\b")
_REGION_REMOTE = re.compile(r"(?i)\b(remote|anywhere|worldwide|distributed)\b")
_LOCATION_REMOTE = re.compile(r"(?i)\b(remote|work from home|wfh|telecommute)\b")
_TITLE_REMOTE = re.compile(r"(?i)\bremote\b")
_DESC_REMOTE = re.compile(r"(?i)\b(fully|100%)\s+remote\b")
_DESC_LABELLED = re.compile(r"(?im)^\s*(location|workplace|work\s+type)\s*:.*\bremote\b")

#: ATS vocabularies collapse onto these three (§4.5.2 rank 1). Lever's ``unspecified``
#: is absent on purpose: it means the customer never answered, so it falls through.
_ATS_WORKPLACE = {
    "remote": "remote",
    "hybrid": "hybrid",
    "onsite": "onsite",
    "on_site": "onsite",
    "inoffice": "onsite",
    "inperson": "onsite",
}


def _workplace_token(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return _ATS_WORKPLACE.get(re.sub(r"[^a-z_]", "", value.casefold()))


def _classify_text(text: str, pattern: re.Pattern[str]) -> tuple[bool, str] | None:
    """Hybrid and on-site are checked first: neither may ever yield ``remote=True``."""
    if _HYBRID.search(text):
        return False, "hybrid"
    if _ONSITE.search(text):
        return False, "onsite"
    if pattern.search(text):
        return True, "remote"
    return None


def _from_ats(job: dict[str, Any]) -> tuple[bool | None, str | None] | None:
    """Rank 1: Ashby ``isRemote``/``workplaceType``, Lever ``workplaceType``, Recruitee
    ``remote``/``hybrid``/``on_site``."""
    workplace = _workplace_token(job.get("workplaceType"))
    if workplace:
        return workplace == "remote", workplace

    for key, resolved in (("remote", "remote"), ("hybrid", "hybrid"), ("on_site", "onsite")):
        if job.get(key) is True:
            return resolved == "remote", resolved

    is_remote = job.get("isRemote")
    if isinstance(is_remote, bool):
        return is_remote, "remote" if is_remote else None
    return None


def detect_remote(
    job: dict[str, Any] | None = None,
    location: Location | str | None = None,
    title: str | None = None,
    description_head: str | None = None,
) -> tuple[bool | None, str | None, str | None]:
    """``(remote, workplaceType, remoteSource)`` (§4.5.2).

    ``job`` is the provider payload (rank 1 reads its structured flags directly, so
    adapters need no translation table). ``description_head`` must be passed **only** when
    ``includeDescription`` is on — rank 5 is defined as opt-in.
    """
    ats = _from_ats(job or {})
    if ats is not None:
        return ats[0], ats[1], "ats"

    raw = location.raw if isinstance(location, Location) else location
    raw = raw or ""

    # Rank 2: what §4.5.1 step 3 stripped, or region-only text that names no place.
    stripped, marker = strip_workplace_markers(raw)
    if marker:
        hit = _classify_text(marker, _REGION_REMOTE)
        if hit:
            return hit[0], hit[1], "location"
    if raw and is_region_only(stripped or raw):
        hit = _classify_text(raw, _REGION_REMOTE)
        if hit:
            return hit[0], hit[1], "location"

    # Rank 3: the raw location string itself.
    if raw:
        hit = _classify_text(raw, _LOCATION_REMOTE)
        if hit:
            return hit[0], hit[1], "location"

    # Rank 4: the job title.
    if title:
        hit = _classify_text(title, _TITLE_REMOTE)
        if hit:
            return hit[0], hit[1], "title"

    # Rank 5: two narrow patterns, first 1,500 characters only.
    if description_head:
        head = description_head[:DESCRIPTION_SCAN_CHARS]
        if _DESC_REMOTE.search(head) or _DESC_LABELLED.search(head):
            return True, "remote", "description"

    # Rank 6.
    return None, None, None
