"""Pure snapshot diff (SPEC v2 §7.4) — the single most-tested piece of the codebase.

Used by B1 (`ats-history-snapshot`) and A8 (`ats-jobs-history`). No IO, no clock: the
caller passes `today`, so a replayed snapshot produces byte-identical events.

Events are built from :data:`EVENT_KEYS` only. Splatting a state record into an event
(v1) leaked `first_seen`, `last_seen` and `h` into every row and inflated the event store
§7.7 budgets at 220 B/event (§7.4 correction 3).
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

EVENT_KEYS = (
    "d",
    "provider",
    "company",
    "job_id",
    "ev",
    "t",
    "loc",
    "dept",
    "url",
    "posted",
    "days_open",
    "changed",
)

#: The fields a `changed` event reports on, and the fields the state hash covers.
CHANGE_FIELDS = ("t", "loc", "dept", "remote")

#: Fields copied from a fetched job into the stored state record.
STATE_FIELDS = ("t", "loc", "dept", "remote", "url", "updated")


def canon(value: Any) -> str:
    """§4.5.6 canonicalisation, applied to whatever the snapshot stored."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (list, tuple)):
        return "|".join(sorted({canon(v) for v in value if v not in (None, "")}))
    return " ".join(str(value).split()).strip().casefold()


def jhash(job: dict[str, Any]) -> str:
    """Default state hash over :data:`CHANGE_FIELDS`.

    Canonicalised so a `None` department cannot hash like the literal string "None" and
    a reordered location array cannot emit a phantom `loc` change (§4.5.6 correction 3).
    Pass a different `hasher` to :func:`diff` when the caller already holds the job's
    §4.5.6 `changeHash`.
    """
    payload = "|".join(canon(job.get(key)) for key in CHANGE_FIELDS)
    return hashlib.sha1(payload.encode(), usedforsecurity=False).hexdigest()[:8]


def _event(
    ev: str,
    jid: str,
    rec: dict[str, Any],
    today: str,
    provider: str,
    company: str,
    *,
    days_open: int | None = None,
    changed: list[str] | None = None,
) -> dict[str, Any]:
    """Build an event from the documented key set only. Never splat state records."""
    return {
        "d": today,
        "provider": provider,
        "company": company,
        "job_id": jid,
        "ev": ev,
        "t": rec.get("t"),
        "loc": rec.get("loc"),
        "dept": rec.get("dept"),
        "url": rec.get("url"),
        "posted": rec.get("posted"),
        "days_open": days_open,
        "changed": changed,
    }


def _days_open(today: str, posted: str | None) -> int | None:
    """`max(0, ...)`: providers backdate, and a future `posted` fed a negative value into
    `median_days_to_fill`, the flagship metric of the paid history Actor (§7.4)."""
    if not posted:
        return None
    try:
        d = date.fromisoformat(posted[:10])
    except ValueError:
        return None
    return max(0, (date.fromisoformat(today) - d).days)


def diff(
    prev: dict[str, dict[str, Any]],
    cur: list[dict[str, Any]],
    today: str,
    provider: str,
    company: str,
    *,
    hasher: Any = jhash,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """prev = stored jobs by id; cur = jobs fetched now. Returns (next_state, events)."""
    events: list[dict[str, Any]] = []
    nxt: dict[str, dict[str, Any]] = {}
    cur_by_id = {str(j["id"]): j for j in cur}
    for jid, j in cur_by_id.items():
        h = hasher(j)
        old = prev.get(jid)
        rec = {
            **{k: j.get(k) for k in STATE_FIELDS},
            "h": h,
            "first_seen": old["first_seen"] if old and "first_seen" in old else today,
            "last_seen": today,
            # Stored as YYYY-MM-DD (§4.5.5); `date.fromisoformat` would raise on the
            # timestamp form, which is what killed a whole bucket's day in v1.
            "posted": ((old or {}).get("posted") or j.get("posted") or today)[:10],
            "posted_src": (old or {}).get("posted_src")
            or ("api" if j.get("posted") else "snapshot"),
        }
        nxt[jid] = rec
        if old is None:
            events.append(_event("added", jid, rec, today, provider, company))
        elif old.get("h") != h:
            changed = [k for k in CHANGE_FIELDS if old.get(k) != rec.get(k)]
            events.append(_event("changed", jid, rec, today, provider, company, changed=changed))
    for jid, old in prev.items():
        if jid not in cur_by_id:
            events.append(
                _event(
                    "removed",
                    jid,
                    old,
                    today,
                    provider,
                    company,
                    days_open=_days_open(today, old.get("posted")),
                )
            )
    return nxt, events
