"""`core.history`: bucket layout, gzip codec, typed views, purge (SPEC v2 §7.3, §10.1)."""

from __future__ import annotations

import gzip
import json
from types import SimpleNamespace

import pytest

from core.history import (
    BUCKETS,
    MAX_VALUE_BYTES,
    HistoryStore,
    bucket_of,
    buckets_for_shard,
    counts_key,
    decode,
    encode,
    events_key,
    from_jsonl,
    shard_of,
    state_key,
    to_jsonl,
)


class FakeStore:
    """A dict with the KeyValueStore surface. Keeps the whole suite offline."""

    def __init__(self, values: dict | None = None):
        self.values = dict(values or {})
        self.content_types: dict[str, str | None] = {}

    async def get_value(self, key, default_value=None):
        return self.values.get(key, default_value)

    async def set_value(self, key, value, content_type=None):
        self.values[key] = value
        self.content_types[key] = content_type

    async def iterate_keys(self):
        for key in list(self.values):
            yield SimpleNamespace(key=key)


def company_state(**jobs):
    return {"as_of": "2026-08-26", "companies": jobs}


# --- bucket / shard layout ---------------------------------------------------


def test_bucket_is_stable_across_processes():
    # crc32, not hash(): PYTHONHASHSEED is per-process, and a reshuffle would strand
    # every company's history in a bucket nothing reads.
    assert bucket_of("greenhouse", "anthropic") == bucket_of("greenhouse", "anthropic")
    assert bucket_of("greenhouse", "anthropic") == 57  # crc32("greenhouse:anthropic") % 64
    assert 0 <= bucket_of("lever", "palantir") < BUCKETS


def test_bucket_separates_providers_sharing_a_slug():
    assert bucket_of("greenhouse", "acme") != bucket_of("lever", "acme")


def test_every_bucket_belongs_to_exactly_one_shard():
    for count in (1, 2, 4, 8):
        owned = [b for shard in range(count) for b in buckets_for_shard(shard, count)]
        assert sorted(owned) == list(range(BUCKETS))
        assert len(set(owned)) == BUCKETS


def test_shard_split_matches_the_spec_table():
    assert buckets_for_shard(0, 4) == list(range(0, 16))
    assert buckets_for_shard(1, 4) == list(range(16, 32))
    assert buckets_for_shard(2, 4) == list(range(32, 48))
    assert buckets_for_shard(3, 4) == list(range(48, 64))
    assert shard_of(63, 4) == 3


def test_keys_are_zero_padded():
    # `/` is not a legal Apify KV key character: it lands in the API URL path unescaped
    # and every write 404s. Locked down here because §7.3 writes the keys with slashes.
    assert state_key(7) == "state.07"
    assert state_key(63) == "state.63"
    assert events_key("2026-08-26", 2) == "events.2026-08-26.2"
    assert counts_key("2026-08-26", 0) == "counts.2026-08-26.0"
    assert not any(
        "/" in k for k in (state_key(0), events_key("2026-08-26", 0), counts_key("2026-08-26", 0))
    )


# --- codec -------------------------------------------------------------------


def test_encode_is_gzipped_json_and_deterministic():
    blob = encode({"a": 1, "b": "é"})
    assert blob[:2] == b"\x1f\x8b"
    assert json.loads(gzip.decompress(blob)) == {"a": 1, "b": "é"}
    # mtime=0 so an unchanged bucket produces identical bytes, which is what makes
    # "did anything change" answerable without decompressing.
    assert encode({"a": 1, "b": "é"}) == blob


def test_encode_refuses_a_value_over_the_kv_body_limit(monkeypatch):
    # The real cap is 8 MiB gzipped (§7.3, under the ~9 MiB API body limit); shrinking it
    # here exercises the guard without building a 20 MB incompressible fixture.
    assert MAX_VALUE_BYTES == 8 * 1024 * 1024
    monkeypatch.setattr("core.history.MAX_VALUE_BYTES", 128)
    with pytest.raises(ValueError, match="over the"):
        encode([{"job": i, "t": f"Engineer {i}"} for i in range(500)])


def test_decode_degrades_a_corrupt_value_instead_of_losing_the_day():
    assert decode(b"not gzip at all", {"companies": {}}) == {"companies": {}}
    assert decode(None, []) == []
    assert decode('{"a":1}', {}) == {"a": 1}


def test_jsonl_roundtrip_drops_a_truncated_last_line():
    rows = [{"d": "2026-08-26", "ev": "added"}, {"d": "2026-08-26", "ev": "removed"}]
    text = to_jsonl(rows)
    assert from_jsonl(text) == rows
    assert from_jsonl(text[:-5]) == rows[:1]
    assert from_jsonl(None) == []


# --- typed views -------------------------------------------------------------


async def test_store_roundtrips_every_view_and_counts_its_io():
    store = HistoryStore(FakeStore())
    await store.put_watchlist([{"provider": "lever", "company": "palantir", "added": "2026-08-26"}])
    await store.put_state(3, company_state(**{"lever:palantir": {"jobs": {"1": {"t": "SWE"}}}}))
    await store.put_events("2026-08-26", 0, [{"ev": "added", "job_id": "1"}])
    await store.put_counts("2026-08-26", 0, [{"d": "2026-08-26", "open": 4}])
    await store.put_meta({"version": 1})

    assert (await store.watchlist())[0]["company"] == "palantir"
    assert (await store.state(3))["companies"]["lever:palantir"]["jobs"]["1"]["t"] == "SWE"
    assert await store.events("2026-08-26", 0) == [{"ev": "added", "job_id": "1"}]
    assert await store.counts("2026-08-26", 0) == [{"d": "2026-08-26", "open": 4}]
    assert (await store.meta())["version"] == 1

    assert store.writes == 5 and store.reads == 5
    assert store.bytes_written > 0


async def test_missing_or_broken_state_reads_as_empty_not_as_a_crash():
    store = HistoryStore(FakeStore({state_key(9): encode({"as_of": "x"})}))
    assert await store.state(9) == {"as_of": None, "companies": {}}
    assert await store.state(10) == {"as_of": None, "companies": {}}
    assert await store.watchlist() == []


async def test_watchlist_drops_rows_missing_a_provider_or_company():
    raw = [{"provider": "lever", "company": "a"}, {"provider": "lever"}, "junk", {"company": "b"}]
    store = HistoryStore(FakeStore({"watchlist": encode(raw)}))
    assert await store.watchlist() == [{"provider": "lever", "company": "a"}]


async def test_store_writes_gzip_content_type():
    backing = FakeStore()
    await HistoryStore(backing).put_meta({"version": 1})
    assert backing.content_types["meta"] == "application/gzip"


# --- purge (§7.3, §15.1 policy 4) --------------------------------------------


async def test_purge_removes_all_traces():
    gh_bucket = bucket_of("greenhouse", "acme")
    lever_bucket = bucket_of("lever", "palantir")
    backing = FakeStore(
        {
            state_key(gh_bucket): encode(
                company_state(**{"greenhouse:acme": {"jobs": {"1": {"t": "SWE"}}}})
            ),
            state_key(lever_bucket): encode(
                company_state(**{"lever:palantir": {"jobs": {"9": {"t": "PM"}}}})
            ),
            events_key("2026-08-26", 0): encode(
                to_jsonl(
                    [
                        {"provider": "greenhouse", "company": "acme", "job_id": "1"},
                        {"provider": "lever", "company": "palantir", "job_id": "9"},
                    ]
                )
            ),
        }
    )
    store = HistoryStore(backing)

    removed = await store.purge("greenhouse", "acme", keys=[events_key("2026-08-26", 0)])

    assert removed == {"companies": 1, "events": 1, "buckets": 1}
    assert (await store.state(gh_bucket))["companies"] == {}
    assert await store.events("2026-08-26", 0) == [
        {"provider": "lever", "company": "palantir", "job_id": "9"}
    ], "an unrelated company must survive a takedown"
    assert (await store.state(lever_bucket))["companies"] != {}


async def test_purge_whole_provider_takes_every_company():
    buckets = {bucket_of("recruitee", slug): slug for slug in ("a", "b", "c")}
    backing = FakeStore(
        {
            state_key(bucket): encode(company_state(**{f"recruitee:{slug}": {"jobs": {}}}))
            for bucket, slug in buckets.items()
        }
    )
    store = HistoryStore(backing)
    removed = await store.purge("recruitee")
    assert removed["companies"] == 3
    for bucket in buckets:
        assert (await store.state(bucket))["companies"] == {}


async def test_jsonl_values_are_stored_as_raw_text_not_as_a_quoted_json_string():
    """§7.3 says `events`/`counts` are JSONL. JSON-dumping the JSONL string escaped every
    quote and newline — 35% bigger on the wire, and anything reading the bytes directly
    got a quoted string instead of lines."""
    backing = FakeStore()
    store = HistoryStore(backing)
    rows = [{"ev": "added", "job_id": str(i)} for i in range(3)]
    await store.put_events("2026-08-26", 0, rows)

    raw = gzip.decompress(backing.values[events_key("2026-08-26", 0)]).decode()
    assert raw.startswith('{"ev":"added"') and raw.count("\n") == 2
    assert '\\"' not in raw
    assert await store.events("2026-08-26", 0) == rows


async def test_a_value_written_by_the_old_quoted_codec_is_still_readable():
    backing = FakeStore({events_key("2026-08-26", 0): encode(to_jsonl([{"ev": "added"}]))})
    assert await HistoryStore(backing).events("2026-08-26", 0) == [{"ev": "added"}]
