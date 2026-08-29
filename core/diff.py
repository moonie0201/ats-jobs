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
    "verified",
)

#: Providers whose single-posting endpoint answers 404 once a posting is closed, giving a
#: `removed` a second signal independent of feed membership (§7.4). Ashby's
#: `posting-api/job-board/{org}/{id}` is 401 and its public page is a client-rendered
#: 200 for any uuid; Recruitee, Rippling and Personio expose nothing usable either, so for
#: those four the feed is the only signal and `verified` stays `None`.
VERIFIABLE = frozenset({"greenhouse", "lever"})

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
    verified: bool | None = None,
) -> dict[str, Any]:
    """Build an event from the documented key set only. Never splat state records.

    `verified` is meaningful on `removed` only. `diff` is pure and always emits `None`; the
    snapshot runner asks the provider's single-posting endpoint and rewrites it to `True`
    (404: confirmed closed), `False` (feed says gone, endpoint unreachable for
    `UNVERIFIED_REMOVAL_AFTER` sweeps) or leaves `None` for providers with no endpoint.
    """
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
        "verified": verified,
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
        old = prev.get(jid)
        if old and j.get("dept") is None and old.get("dept"):
            # A null `dept` is *our* gap, never the employer's edit (§5.1): a failed
            # `/departments` call must not erase a department the state already knows, or
            # the `removed` a later day builds from `old` ships without it.
            j = {**j, "dept": old["dept"]}
        h = hasher(j)
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
        elif old.get("h") != h and (
            changed := [
                k
                for k in CHANGE_FIELDS
                if canon(old.get(k)) != canon(rec.get(k))
                # `dept` null on either side is *our* gap, not the employer's edit: it was
                # null on every Greenhouse row until the list-only `/departments` call
                # existed (§5.1). The backfill must not emit `changed: ["dept"]` for a
                # whole board; a known department is carried forward above, so an outage
                # leaves the state and `h` untouched and the next real edit diffs cleanly.
                and (k != "dept" or (canon(old.get(k)) and canon(rec.get(k))))
            ]
        ):
            # A hash-scheme change (a field added to CHANGE_FIELDS) rehashes every stored
            # job at once. Without the walrus guard that emits `changed: []` for all of
            # them — one semantically empty event per job in the whole store (H1 L2).
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
    # §7.5: Greenhouse and Lever mint a **new** job id on a re-post, so the common repost
    # arrives as removed+added rather than as a `changed`. Billing that pair as a fill
    # would make `median_days_open_of_removed` — the flagship history metric — count every
    # repost as a hire, so the fill signal is withheld when the same canonical job
    # (title, location, department, remote) is still open under a different id (H1 M6).
    # Not a hash comparison: `h` covers `dept`, which is null on every Greenhouse record
    # stored before the `/departments` call existed and on any day that call fails, so a
    # repost across such a day hashed differently on each side and was billed as a fill.
    added = [nxt[e["job_id"]] for e in events if e["ev"] == "added"]
    if added:
        # ponytail: O(added x removed) per company; index by (t, loc, remote) if a board
        # ever churns thousands of jobs in one day.
        for event in events:
            if event["ev"] == "removed" and any(
                _same_job(prev[event["job_id"]], new) for new in added
            ):
                event["days_open"] = None
    return nxt, events


def _same_job(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """§7.5 canonical identity; `dept` counts only when both sides know it."""
    return all(
        canon(a.get(k)) == canon(b.get(k))
        for k in CHANGE_FIELDS
        if k != "dept" or (canon(a.get(k)) and canon(b.get(k)))
    )
