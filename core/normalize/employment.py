"""Employment type (SPEC v2 §4.5.4).

Normalizes to ``full_time | part_time | contract | temporary | internship | other | null``
and exports where the answer came from in ``employmentTypeSource`` (``ats`` | ``title`` |
``null``). The provenance matters because ``strictEmploymentType`` — a filter that changes
what gets billed — is defined as ``employmentTypeSource == "ats"``.

The provider vocabularies are merged into one table: no token means different things on
two providers, so six tables would be six places to forget a fix.
"""

from __future__ import annotations

import re
from typing import Any

FULL_TIME = "full_time"
PART_TIME = "part_time"
CONTRACT = "contract"
TEMPORARY = "temporary"
INTERNSHIP = "internship"
OTHER = "other"

#: Keys are the §4.5.4 canonical form: casefold, then strip everything but ``[a-z]``.
_TYPES: dict[str, str] = {
    # Ashby, Lever, Recruitee, Personio
    "fulltime": FULL_TIME,
    "parttime": PART_TIME,
    "contract": CONTRACT,
    "contractor": CONTRACT,
    "freelance": CONTRACT,
    "temporary": TEMPORARY,
    "intern": INTERNSHIP,
    "internship": INTERNSHIP,
    "permanent": FULL_TIME,
    "trainee": INTERNSHIP,
    "workingstudent": PART_TIME,
    "apprenticeship": INTERNSHIP,
    "volunteer": OTHER,
    # Rippling — ``employmentType.id`` holds the human string and ``.label`` the machine
    # token, the inverse of every other provider (V2 T-C2). Both forms map here so the
    # adapter cannot break by reading the wrong one.
    "salariedft": FULL_TIME,
    "salariedfulltime": FULL_TIME,
    "salariedpt": PART_TIME,
    "salariedparttime": PART_TIME,
    "hourlyft": FULL_TIME,
    "hourlyfulltime": FULL_TIME,
    "hourlypt": PART_TIME,
    "hourlyparttime": PART_TIME,
    "temp": TEMPORARY,
}

#: §4.5.4 title fallback, evaluated in order.
_TITLE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(intern|internship|praktikum|working student|werkstudent)\b"), INTERNSHIP),
    (re.compile(r"(?i)\b(contract(or)?|freelance|b2b)\b"), CONTRACT),
    (re.compile(r"(?i)\bpart[- ]time\b"), PART_TIME),
    (re.compile(r"(?i)\b(temporary|maternity cover|fixed[- ]term)\b"), TEMPORARY),
)


def _canon(value: str) -> str:
    return re.sub(r"[^a-z]", "", value.casefold())


def _raw_value(value: Any) -> str | None:
    """Flatten a provider value to its string form.

    Rippling sends ``{"label": "SALARIED_FT", "id": "Salaried, full-time"}``; ``id`` is
    read first per §4.5.4, with ``label`` as the fallback so a shape change degrades to
    ``other`` rather than to ``null``.
    """
    if isinstance(value, dict):
        for key in ("id", "label", "name", "value", "code"):
            inner = value.get(key)
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def employment_from_title(title: str | None) -> str | None:
    """§4.5.4 title fallback. Used only when the ATS reports nothing."""
    if not title:
        return None
    for pattern, resolved in _TITLE_RULES:
        if pattern.search(title):
            return resolved
    return None


def detect_employment(
    value: Any = None,
    title: str | None = None,
    schedule: Any = None,
) -> tuple[str | None, str | None, str | None]:
    """``(employmentType, employmentTypeRaw, employmentTypeSource)``.

    ``value`` is the provider's own field (Ashby ``employmentType``, Lever
    ``categories.commitment``, Recruitee ``employment_type_code``, Rippling
    ``employmentType``, Personio ``employmentType``); ``schedule`` is Personio's
    ``full-time``/``part-time`` refinement. An unknown non-empty value is ``other`` with
    the original preserved in ``employmentTypeRaw``.
    """
    raw = _raw_value(value)
    schedule_raw = _raw_value(schedule)

    if raw is None:
        resolved = employment_from_title(title)
        if resolved:
            return resolved, None, "title"
        return None, None, None

    resolved = _TYPES.get(_canon(raw), OTHER)
    # Personio: ``employmentType: permanent`` + ``schedule: part-time`` is a part-timer.
    if schedule_raw:
        refined = _TYPES.get(_canon(schedule_raw))
        if refined in (FULL_TIME, PART_TIME) and resolved in (FULL_TIME, OTHER):
            resolved = refined
    return resolved, raw, "ats"
