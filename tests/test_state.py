"""§4.5.6 / §4.1 onlyNewJobs state. The KV store is injected — no network, no Actor."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from core.state import STATE_VALUE_KEY, SeenState

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


class FakeStore:
    """A named key-value store, minus Apify."""

    def __init__(self, value: Any = None, *, fail: bool = False):
        self.values: dict[str, Any] = {STATE_VALUE_KEY: value} if value is not None else {}
        self.fail = fail
        self.writes = 0

    async def get_value(self, key: str, default_value: Any = None) -> Any:
        if self.fail:
            raise RuntimeError("store unavailable")
        return self.values.get(key, default_value)

    async def set_value(self, key: str, value: Any, content_type: str | None = None) -> None:
        if self.fail:
            raise RuntimeError("store unavailable")
        self.writes += 1
        self.values[key] = value


async def test_first_run_stores_the_baseline_and_calls_everything_new():
    store = FakeStore()
    state = await SeenState(store).load()
    assert state.seen == {}
    assert state.is_new("greenhouse:anthropic:1")
    state.mark("greenhouse:anthropic:1", "abc12345", now=NOW)
    await state.save()
    assert store.values[STATE_VALUE_KEY]["greenhouse:anthropic:1"] == {
        "firstSeen": "2026-08-26T12:00:00Z",
        "lastSeen": "2026-08-26T12:00:00Z",
        "changeHash": "abc12345",
    }


async def test_second_run_sees_known_ids_and_keeps_first_seen():
    store = FakeStore(
        {
            "greenhouse:anthropic:1": {
                "firstSeen": "2026-08-01T00:00:00Z",
                "lastSeen": "2026-08-25T00:00:00Z",
                "changeHash": "abc12345",
            }
        }
    )
    state = await SeenState(store).load()
    assert not state.is_new("greenhouse:anthropic:1")
    assert state.is_new("greenhouse:anthropic:2")
    assert state.mark("greenhouse:anthropic:1", "zzz", now=NOW) is False
    assert state.first_seen("greenhouse:anthropic:1") == "2026-08-01T00:00:00Z"
    assert state.seen["greenhouse:anthropic:1"]["lastSeen"] == "2026-08-26T12:00:00Z"
    assert state.seen["greenhouse:anthropic:1"]["changeHash"] == "zzz"


async def test_mark_reports_newness_before_the_write():
    state = await SeenState(FakeStore()).load()
    assert state.mark("a", now=NOW) is True
    assert state.mark("a", now=NOW) is False


async def test_prune_forgets_ids_past_the_retention_window():
    old = (NOW - timedelta(days=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = (NOW - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store = FakeStore(
        {
            "old": {"firstSeen": old, "lastSeen": old},
            "recent": {"firstSeen": recent, "lastSeen": recent},
        }
    )
    state = await SeenState(store).load()
    assert state.prune(90, now=NOW) == 1
    assert set(state.seen) == {"recent"}


async def test_retention_zero_keeps_everything():
    old = "2020-01-01T00:00:00Z"
    store = FakeStore({"old": {"firstSeen": old, "lastSeen": old}})
    state = await SeenState(store).load()
    assert state.prune(0, now=NOW) == 0
    assert set(state.seen) == {"old"}


async def test_prune_tolerates_records_with_no_last_seen():
    store = FakeStore({"broken": {"firstSeen": "2026-08-01T00:00:00Z"}})
    state = await SeenState(store).load()
    assert state.prune(90, now=NOW) == 1


async def test_a_corrupt_value_is_an_empty_baseline_not_a_crash():
    for value in ("garbage", [1, 2, 3], {"id": "not-a-record"}):
        state = await SeenState(FakeStore(value)).load()
        assert state.seen == {}


async def test_an_unreadable_store_degrades_to_a_baseline_run():
    state = await SeenState(FakeStore(fail=True)).load()
    assert state.seen == {}
    assert state.loaded


async def test_a_failed_write_does_not_fail_the_run():
    store = FakeStore()
    state = await SeenState(store).load()
    state.mark("a", now=NOW)
    store.fail = True
    await state.save()  # logs and returns
    assert store.writes == 0


async def test_open_uses_the_injected_opener_with_the_state_key_as_store_name():
    seen: list[str] = []
    store = FakeStore({"x": {"firstSeen": "2026-08-01T00:00:00Z", "lastSeen": "x"}})

    async def opener(state_key: str):
        seen.append(state_key)
        return store

    state = await SeenState.open("ats-jobs-state-default", opener=opener)
    assert seen == ["ats-jobs-state-default"]
    assert not state.is_new("x")


async def test_state_without_a_store_is_a_working_no_op():
    state = SeenState()
    await state.load()
    state.mark("a", now=NOW)
    await state.save()
    assert state.seen.keys() == {"a"}


# --- V1 H2 / V3 S10 -------------------------------------------------------------------


async def test_load_directory_offers_the_http_sources_only_with_a_client():
    """The two CDN sources are the only ones a customer account can actually reach; with
    no client they were silently dropped and the directory was always empty (V1 H2)."""
    from core.directory import load_directory

    asked: list[str] = []

    class FakeClient:
        async def get(self, url, **kwargs):
            asked.append(url)
            raise RuntimeError("offline")

    async def no_kv(*, name):
        raise RuntimeError("no store")

    empty = await load_directory(None, kv_opener=no_kv, baked_path=Path("/nonexistent"))
    assert asked == [] and len(empty) == 0

    await load_directory(FakeClient(), kv_opener=no_kv, baked_path=Path("/nonexistent"))
    assert len(asked) == 2 and all(url.startswith("https://") for url in asked)
