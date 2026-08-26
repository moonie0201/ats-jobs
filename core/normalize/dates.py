"""Date normalization (SPEC v2 §4.5.5).

Everything the dataset emits is ISO 8601 UTC with a ``Z``. A naive date becomes midnight
UTC; relative text ("Posted 30+ Days Ago", Workday) is not a date and yields ``None``
rather than a guess. The history state stores ``YYYY-MM-DD`` (§7.3) — that is
:func:`to_date`, and the two must not drift apart, which is why both live here.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

#: Epoch values at or above this are milliseconds (Lever ``createdAt``); below, seconds.
#: 1e11 seconds is the year 5138, so the split is unambiguous for any real posting.
_MS_THRESHOLD = 1e11


def _to_datetime(value: object) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day, tzinfo=UTC)
    elif isinstance(value, int | float):
        parsed = _from_epoch(float(value))
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.lstrip("-").isdigit():
            return _from_epoch(float(text))
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _from_epoch(number: float) -> datetime | None:
    if number <= 0:
        return None
    if abs(number) >= _MS_THRESHOLD:
        number /= 1000.0
    try:
        return datetime.fromtimestamp(number, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def to_iso_utc(value: object) -> str | None:
    """Any provider date form -> ``2026-08-19T00:00:00Z``; unparsable -> ``None``.

    Accepts ISO strings (with or without offset), ``YYYY-MM-DD``, epoch seconds or
    milliseconds as a number or digit string, ``date`` and ``datetime``.
    """
    parsed = _to_datetime(value)
    if parsed is None:
        return None
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_date(value: object) -> str | None:
    """Any provider date form -> ``2026-08-19``, the §7.3 storage format."""
    parsed = _to_datetime(value)
    return parsed.astimezone(UTC).strftime("%Y-%m-%d") if parsed else None


def pick_date(*candidates: tuple[str, object]) -> tuple[str | None, str | None]:
    """First parsable ``(source_name, value)`` pair -> ``(iso, source_name)``.

    Greenhouse's ``first_published`` -> ``updated_at`` fallback is the reason this exists:
    ``postedAtSource`` has to name the field the date actually came from.
    """
    for source, value in candidates:
        iso = to_iso_utc(value)
        if iso:
            return iso, source
    return None, None


def utc_now_iso() -> str:
    """``scrapedAt`` / ``firstSeenAt`` stamp."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
