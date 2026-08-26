"""`onlyNewJobs` state: seen job ids in the user's own named KV store (SPEC v2 §4.5.6).

Shape, per §4.5.6: ``{id: {"firstSeen": iso, "lastSeen": iso, "changeHash": str|None}}``.
`stateKey` is the **store name**, not a resource input, so the Actor keeps limited
permissions and creates the store itself (§4.1).

The store is injected rather than imported so tests run offline against a dict.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: Key inside the named store. One value; the store name is what the user varies.
STATE_VALUE_KEY = "seen"


class KeyValueStoreLike(Protocol):
    async def get_value(self, key: str, default_value: Any = None) -> Any: ...

    async def set_value(self, key: str, value: Any, content_type: str | None = None) -> None: ...


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _default_opener(state_key: str) -> KeyValueStoreLike:
    from apify import Actor

    return await Actor.open_key_value_store(name=state_key)


class SeenState:
    """Load once, decide `isNew` in memory, prune, save once."""

    __slots__ = ("seen", "_store", "_key", "loaded")

    def __init__(self, store: KeyValueStoreLike | None = None, *, key: str = STATE_VALUE_KEY):
        self._store = store
        self._key = key
        self.seen: dict[str, dict[str, Any]] = {}
        self.loaded = False

    @classmethod
    async def open(
        cls,
        state_key: str,
        *,
        opener: Any | None = None,
        key: str = STATE_VALUE_KEY,
    ) -> SeenState:
        store = await (opener or _default_opener)(state_key)
        state = cls(store, key=key)
        await state.load()
        return state

    async def load(self) -> SeenState:
        """A missing or corrupt value is an empty baseline, never a crash: the first run
        stores the baseline and outputs everything (§4.1)."""
        if self._store is None:
            self.loaded = True
            return self
        try:
            value = await self._store.get_value(self._key)
        except Exception as exc:
            logger.warning("could not read job-id state: %s", exc)
            value = None
        if not isinstance(value, dict):
            value = {}
        self.seen = {str(k): v for k, v in value.items() if isinstance(v, dict)}
        self.loaded = True
        return self

    def is_new(self, job_id: str) -> bool:
        """`isNew=true` when the id is absent from the state (§4.5.6)."""
        return job_id not in self.seen

    def first_seen(self, job_id: str) -> str | None:
        record = self.seen.get(job_id)
        return record.get("firstSeen") if record else None

    def mark(
        self,
        job_id: str,
        change_hash: str | None = None,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Record a sighting. Returns whether it was new *before* this call."""
        stamp = _now_iso(now)
        record = self.seen.get(job_id)
        new = record is None
        if record is None:
            record = self.seen[job_id] = {"firstSeen": stamp}
        record["lastSeen"] = stamp
        record["changeHash"] = change_hash
        return new

    def prune(self, retention_days: int, *, now: datetime | None = None) -> int:
        """Drop ids not seen for `stateRetentionDays`. 0 keeps everything (§4.1).

        Clamped at both ends before it is used. A **negative** value put the cutoff a day
        in the *future*, so every id was stale and `save()` then persisted the empty dict:
        the next run saw no baseline, treated every job as new and charged for all of them
        (V1 H2). A huge value overflowed `timedelta(days=...)` and failed the run outright
        (V3 S21). The schema's `minimum: 0` binds the Console form only — API, CLI and
        `call-actor` callers reach here unfiltered.
        """
        retention_days = min(max(0, int(retention_days or 0)), 3650)
        if not retention_days:
            return 0
        cutoff = (now or datetime.now(UTC)).astimezone(UTC) - timedelta(days=retention_days)
        cutoff_iso = _now_iso(cutoff)
        stale = [
            job_id
            for job_id, record in self.seen.items()
            if str(record.get("lastSeen") or "") < cutoff_iso
        ]
        for job_id in stale:
            del self.seen[job_id]
        return len(stale)

    async def save(self) -> None:
        if self._store is None:
            return
        try:
            await self._store.set_value(self._key, self.seen)
        except Exception as exc:
            # A lost write costs the next run a re-baseline; it must not fail this one.
            logger.warning("could not persist job-id state: %s", exc)
