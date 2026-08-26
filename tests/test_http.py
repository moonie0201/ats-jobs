"""§5.12 failure table + the per-host rate cap. No network: respx serves every call."""

from __future__ import annotations

import httpx
import pytest
import respx

from core.http import (
    DEFAULT_HEADERS,
    USER_AGENT,
    Client,
    HttpError,
    NotFound,
    ParseError,
    RateLimited,
    Timeout,
    TokenBucket,
    backoff_delay,
    make_client,
)

URL = "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs"


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client(sleeps: list[float]):
    """A client on a fake clock: every backoff and every rate-cap wait is instant, and
    `sleeps` is the exact schedule the run would have slept in production."""
    clock = [0.0]

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        clock[0] += delay

    return make_client(timeout_secs=5, sleep=fake_sleep, clock=lambda: clock[0])


def test_user_agent_is_honest_and_carries_no_placeholder():
    assert USER_AGENT.startswith("ats-jobs-scraper/0.1 (+https://github.com/")
    assert "<" not in USER_AGENT
    assert DEFAULT_HEADERS["Accept-Encoding"] == "gzip"


@respx.mock
async def test_sends_gzip_and_user_agent(client: Client):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={"jobs": []}))
    assert await client.get_json(URL) == {"jobs": []}
    request = route.calls[0].request
    assert request.headers["user-agent"] == USER_AGENT
    assert "gzip" in request.headers["accept-encoding"]


@respx.mock
async def test_404_is_not_found_and_is_never_retried(client: Client):
    route = respx.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await client.get(URL)
    assert route.call_count == 1


@respx.mock
async def test_307_is_not_found_for_personio(client: Client):
    """Personio answers an unknown company with 307; following it would yield a 200."""
    url = "https://nosuch.jobs.personio.de/xml"
    route = respx.get(url).mock(return_value=httpx.Response(307, headers={"location": "/"}))
    with pytest.raises(NotFound):
        await client.get(url)
    assert route.call_count == 1


@respx.mock
async def test_429_backs_off_three_times_then_gives_up(client: Client, sleeps: list[float]):
    route = respx.get(URL).mock(return_value=httpx.Response(429))
    with pytest.raises(RateLimited):
        await client.get(URL)
    assert route.call_count == 4  # first attempt + 3 retries
    assert len(sleeps) == 3
    for expected, actual in zip((1.0, 2.0, 4.0), sleeps, strict=True):
        assert 0.75 * expected <= actual <= 1.25 * expected


@respx.mock
async def test_429_honours_retry_after(client: Client, sleeps: list[float]):
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "7"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    assert await client.get_json(URL) == {"ok": True}
    assert sleeps == [7.0]


@respx.mock
async def test_5xx_retries_then_http_error(client: Client):
    route = respx.get(URL).mock(return_value=httpx.Response(503))
    with pytest.raises(HttpError) as excinfo:
        await client.get(URL)
    assert route.call_count == 4
    assert excinfo.value.status == "http_error"


@respx.mock
async def test_5xx_recovers_on_retry(client: Client):
    respx.get(URL).mock(side_effect=[httpx.Response(500), httpx.Response(200, json={"jobs": [1]})])
    assert await client.get_json(URL) == {"jobs": [1]}


@respx.mock
async def test_timeout_retries_exactly_once(client: Client):
    route = respx.get(URL).mock(side_effect=httpx.ReadTimeout("too slow"))
    with pytest.raises(Timeout) as excinfo:
        await client.get(URL)
    assert route.call_count == 2
    assert excinfo.value.status == "timeout"


@respx.mock
async def test_malformed_body_retries_once_then_parse_error(client: Client):
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=b"{truncated"))
    with pytest.raises(ParseError) as excinfo:
        await client.get_json(URL)
    assert route.call_count == 2
    assert excinfo.value.status == "parse_error"


@respx.mock
async def test_custom_parser_inherits_the_retry_rule(client: Client):
    """Personio's XML goes through the same door as everyone's JSON."""
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, content=b"<broken"),
            httpx.Response(200, content=b"<workzag-jobs/>"),
        ]
    )

    def parse(response: httpx.Response) -> str:
        text = response.text
        if not text.endswith(">"):
            raise ValueError("truncated xml")
        return text

    assert await client.get_json(URL, parse=parse) == "<workzag-jobs/>"


def test_lever_is_capped_at_one_rps_and_everyone_else_at_two(client: Client):
    assert client.bucket("https://api.lever.co/v0/postings/palantir").rate == 1.0
    assert client.bucket("https://api.eu.lever.co/v0/postings/palantir").rate == 1.0
    assert client.bucket(URL).rate == 2.0
    assert client.bucket("https://api.ashbyhq.com/posting-api/job-board/x").rate == 2.0


def test_buckets_are_per_host_not_per_company(client: Client):
    assert client.bucket(URL) is client.bucket(
        "https://boards-api.greenhouse.io/v1/boards/stripe/jobs"
    )
    assert client.bucket(URL) is not client.bucket("https://api.lever.co/v0/postings/x")


async def test_token_bucket_paces_at_the_host_rate():
    clock = [0.0]
    waits: list[float] = []

    async def sleep(delay: float) -> None:
        waits.append(delay)
        clock[0] += delay

    bucket = TokenBucket(2.0, now=lambda: clock[0])
    for _ in range(4):
        await bucket.acquire(sleep)

    # One second of burst, then a steady 2 rps.
    assert waits == [0.5, 0.5]


async def test_token_bucket_at_one_rps_waits_a_full_second():
    clock = [0.0]
    waits: list[float] = []

    async def sleep(delay: float) -> None:
        waits.append(delay)
        clock[0] += delay

    bucket = TokenBucket(1.0, now=lambda: clock[0])
    for _ in range(3):
        await bucket.acquire(sleep)

    assert waits == [1.0, 1.0]


def test_backoff_is_one_two_four_with_jitter():
    for attempt, base in ((1, 1.0), (2, 2.0), (3, 4.0)):
        for _ in range(50):
            delay = backoff_delay(attempt)
            assert 0.75 * base <= delay <= 1.25 * base
