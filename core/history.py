"""The `ats-history` named key-value store: bucket layout, gzip codec, purge (SPEC v2 §7.3).

Named store ⇒ never deleted by retention on any plan, which is the whole point: §7 says a
day not collected is lost forever, so the store outliving the run that wrote it is the
moat's only physical requirement.

Every value is gzipped JSON and must stay under 8 MiB (§7.3). Keys::

    watchlist                    [{"provider","company","site?","added","source"}]
    state.{bucket:02d}           64 buckets, bucket = crc32(f"{provider}:{company}") % 64
    events.{YYYY-MM-DD}.{shard}  JSONL per shard per day
    counts.{YYYY-MM-DD}.{shard}  JSONL, one line per company, written once per run
    meta                         {"last_run":{shard: date}, "ok","failed",...}

**Deviation from §7.3, forced by the platform:** the spec writes these keys with `/`
separators. An Apify key-value-store key goes into the API URL path unescaped, so a `/`
produces `404 there is no API endpoint at this URL` on the very first `state/00` write —
measured, not theorised. `.` is in the allowed key charset and, unlike `-`, cannot be
confused with the dashes inside a date. **A8 must build its keys with the functions below
rather than by formatting §7.3's strings**, or it will read keys that do not exist.

The store is injected rather than imported, same as :mod:`core.state`, so the snapshot
runner and its tests share one code path and the tests stay offline.
"""

from __future__ import annotations

import gzip
import json
import logging
import zlib
from typing import Any, Protocol

logger = logging.getLogger(__name__)

STORE_NAME = "ats-history"

#: §7.3. Not configurable: the bucket number is baked into every key already written, so
#: changing it would strand the history it exists to protect.
BUCKETS = 64

#: §7.3 — the API body limit is ~9 MiB; 8 MiB leaves room for the envelope.
MAX_VALUE_BYTES = 8 * 1024 * 1024

WATCHLIST_KEY = "watchlist"
META_KEY = "meta"


class KeyValueStoreLike(Protocol):
    async def get_value(self, key: str, default_value: Any = None) -> Any: ...

    async def set_value(self, key: str, value: Any, content_type: str | None = None) -> None: ...

    #: `purge()` needs the event keys to rewrite, and there is no other way to enumerate a
    #: year of `events.{date}.{shard}`. Without it the §15.1 takedown path is unreachable.
    def iterate_keys(self) -> Any: ...


def bucket_of(provider: str, company: str) -> int:
    """§7.3: ``crc32(f"{provider}:{company}") % 64``. Stable across processes and versions
    — unlike :func:`hash`, which is salted per process and would reshuffle every company
    into a new bucket on every run."""
    return zlib.crc32(f"{provider}:{company}".encode()) % BUCKETS


def shard_of(bucket: int, shard_count: int = 4) -> int:
    """§7.2: shard 0 owns buckets 00-15, shard 1 16-31, and so on."""
    return min(shard_count - 1, bucket * shard_count // BUCKETS)


def buckets_for_shard(shard: int, shard_count: int = 4) -> list[int]:
    return [b for b in range(BUCKETS) if shard_of(b, shard_count) == shard]


#: Path separator inside a key. Not `/`: see the module docstring.
SEP = "."


def state_key(bucket: int) -> str:
    return f"state{SEP}{bucket:02d}"


def events_key(day: str, shard: int) -> str:
    return f"events{SEP}{day}{SEP}{shard}"


def counts_key(day: str, shard: int) -> str:
    """Per day **and** shard, like `events`.

    §7.3 keyed this on the month, which made `finalize()` read-modify-write a value that
    grows all month (38 MB raw / ~267 MB resident at §7.8's 15,000-company split trigger,
    inside a 512 MB container) and gave two overlapping shards a lost-update race. Keyed
    per run it is written once and never read back, and A8 already loads day/shard for
    `events` (H1 M4/M5).
    """
    return f"counts{SEP}{day}{SEP}{shard}"


def _compress(text: str) -> bytes:
    """`mtime=0` so an unchanged bucket produces identical bytes."""
    blob = gzip.compress(text.encode(), mtime=0)
    if len(blob) > MAX_VALUE_BYTES:
        raise ValueError(f"value is {len(blob)} bytes gzipped, over the {MAX_VALUE_BYTES} cap")
    return blob


def encode(value: Any) -> bytes:
    """JSON -> gzip. `separators` because 70 keys x 4,000 jobs pays for every space."""
    return _compress(json.dumps(value, separators=(",", ":"), ensure_ascii=False))


def encode_text(text: str) -> bytes:
    """Raw text -> gzip, for the JSONL values (`events`, `counts`).

    Not :func:`encode`: JSON-dumping a JSONL string escapes every quote and newline in
    it, which inflated the first live `events` value by ~35% and — worse — meant the
    stored bytes were a quoted JSON string where §7.3 documents JSONL, so any reader that
    did not go through this module read the wrong thing.
    """
    return _compress(text)


def decode(blob: Any, default: Any) -> Any:
    """Whatever the store handed back -> Python. A corrupt value degrades to ``default``.

    Raising here would cost the whole day for one bad key, which §7.4's corrections exist
    to prevent; the caller sees `default` and rewrites the key on this run.
    """
    if blob is None:
        return default
    if isinstance(blob, (bytes, bytearray)):
        try:
            return json.loads(gzip.decompress(blob))
        except (OSError, ValueError) as exc:
            logger.warning("unreadable history value (%s); treating as empty", exc)
            return default
    if isinstance(blob, str):
        try:
            return json.loads(blob)
        except ValueError:
            return default
    return blob


def decode_text(blob: Any, default: str = "") -> str:
    if blob is None:
        return default
    if isinstance(blob, (bytes, bytearray)):
        try:
            text = gzip.decompress(blob).decode()
        except (OSError, ValueError) as exc:
            logger.warning("unreadable history text (%s); treating as empty", exc)
            return default
    elif isinstance(blob, str):
        text = blob
    else:
        return default
    if text.startswith('"'):
        # Written by the pre-`encode_text` codec as a JSON-quoted string. Unwrap once
        # rather than losing a day of events to a codec change.
        try:
            return json.loads(text)
        except ValueError:
            pass
    return text


def to_jsonl(rows: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(r, separators=(",", ":"), ensure_ascii=False) for r in rows)


def from_jsonl(text: Any) -> list[dict[str, Any]]:
    """A truncated last line is dropped, not fatal (an interrupted write is survivable)."""
    if not isinstance(text, str):
        return []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


class HistoryStore:
    """Typed accessors over the §7.3 key layout. Counts its own IO so the runner's budget
    guard has real numbers instead of a guess."""

    __slots__ = ("_store", "reads", "writes", "bytes_written")

    def __init__(self, store: KeyValueStoreLike):
        self._store = store
        self.reads = 0
        self.writes = 0
        self.bytes_written = 0

    @classmethod
    async def open(cls, name: str = STORE_NAME, opener: Any = None) -> HistoryStore:
        if opener is None:
            from apify import Actor

            async def open_named(store_name: str) -> KeyValueStoreLike:
                return await Actor.open_key_value_store(name=store_name)

            opener = open_named
        return cls(await opener(name))

    async def get(self, key: str, default: Any = None) -> Any:
        self.reads += 1
        return decode(await self._store.get_value(key), default)

    async def put(self, key: str, value: Any) -> int:
        return await self._write(key, encode(value))

    async def get_text(self, key: str) -> str:
        self.reads += 1
        return decode_text(await self._store.get_value(key))

    async def put_text(self, key: str, text: str) -> int:
        return await self._write(key, encode_text(text))

    async def _write(self, key: str, blob: bytes) -> int:
        await self._store.set_value(key, blob, content_type="application/gzip")
        self.writes += 1
        self.bytes_written += len(blob)
        return len(blob)

    # --- typed views (§7.3) ---

    async def watchlist(self) -> list[dict[str, Any]]:
        rows = await self.get(WATCHLIST_KEY, [])
        return [r for r in rows if isinstance(r, dict) and r.get("provider") and r.get("company")]

    async def put_watchlist(self, rows: list[dict[str, Any]]) -> int:
        return await self.put(WATCHLIST_KEY, rows)

    async def state(self, bucket: int) -> dict[str, Any]:
        value = await self.get(state_key(bucket), {})
        if not isinstance(value, dict) or not isinstance(value.get("companies"), dict):
            return {"as_of": None, "companies": {}}
        return value

    async def put_state(self, bucket: int, value: dict[str, Any]) -> int:
        return await self.put(state_key(bucket), value)

    async def events(self, day: str, shard: int) -> list[dict[str, Any]]:
        return from_jsonl(await self.get_text(events_key(day, shard)))

    async def put_events(self, day: str, shard: int, rows: list[dict[str, Any]]) -> int:
        return await self.put_text(events_key(day, shard), to_jsonl(rows))

    async def counts(self, day: str, shard: int) -> list[dict[str, Any]]:
        return from_jsonl(await self.get_text(counts_key(day, shard)))

    async def put_counts(self, day: str, shard: int, rows: list[dict[str, Any]]) -> int:
        return await self.put_text(counts_key(day, shard), to_jsonl(rows))

    async def keys(self, prefix: str = "") -> list[str]:
        """Every key in the store, optionally filtered by prefix. Used by :meth:`purge`."""
        found: list[str] = []
        async for info in self._store.iterate_keys():
            key = getattr(info, "key", info)
            if isinstance(key, str) and key.startswith(prefix):
                found.append(key)
        self.reads += 1
        return found

    async def meta(self) -> dict[str, Any]:
        value = await self.get(META_KEY, {})
        return value if isinstance(value, dict) else {}

    async def put_meta(self, value: dict[str, Any]) -> int:
        return await self.put(META_KEY, value)

    # --- takedown (§7.3, §15.1 policy 4: honoured within 48 hours) ---

    async def purge(
        self, provider: str, company: str | None = None, *, keys: list[str] | None = None
    ) -> dict[str, int]:
        """Drop a company (or a whole provider) from every state bucket and event file.

        "Dropped from `ats-history` on the next run" was a sentence with no implementation
        behind it (V1 L9): disabling an adapter stops *collection*, it does not touch the
        rows already stored across 64 buckets and a year of per-day event files. `keys`
        lets the caller hand in the event keys to rewrite (from `iterate_keys`); when it is
        omitted only the state buckets are swept, which is what makes `open_now` and every
        current-state read go blank immediately.
        """
        prefix = f"{provider}:"
        target = f"{provider}:{company}" if company else None
        removed = {"companies": 0, "events": 0, "buckets": 0}

        for bucket in range(BUCKETS):
            state = await self.state(bucket)
            companies = state["companies"]
            doomed = [
                k for k in companies if (k == target if target else str(k).startswith(prefix))
            ]
            if not doomed:
                continue
            for key in doomed:
                companies.pop(key, None)
            removed["companies"] += len(doomed)
            removed["buckets"] += 1
            await self.put_state(bucket, state)

        for key in keys or []:
            if not key.startswith(f"events{SEP}"):
                continue
            rows = from_jsonl(await self.get_text(key))
            kept = [
                r
                for r in rows
                if not (
                    r.get("provider") == provider
                    and (company is None or r.get("company") == company)
                )
            ]
            if len(kept) != len(rows):
                removed["events"] += len(rows) - len(kept)
                await self.put_text(key, to_jsonl(kept))

        return removed
