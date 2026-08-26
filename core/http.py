"""Shared HTTP client: gzip, honest UA, per-host token bucket, retry/backoff.

SPEC v2 §5 (transport rules common to every adapter) and §5.12 (failure table).

The retry policy is ~40 lines here instead of `tenacity` (§9.3 rejects it), and the
per-host rate cap lives in the client rather than in each adapter because four of the
six providers share one host: a cap enforced per adapter instance would be multiplied
by `maxConcurrency` and break the politeness ceiling the legal posture rests on
(§5.12, §14 R21).
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

#: `<owner>` in §5's UA string is a repo placeholder; it is filled at runtime so the
#: header never ships a literal angle-bracket token.
REPO_OWNER = os.environ.get("ATS_REPO_OWNER", "ats-jobs")
USER_AGENT = f"ats-jobs-scraper/0.1 (+https://github.com/{REPO_OWNER}/ats-jobs)"

DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    # Greenhouse and Ashby honour it — Stripe's board drops 4.48 MB to 750 KB (§5.1).
    "Accept-Encoding": "gzip",
    "Accept": "application/json, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.8",
}

#: Requests per second per host. Lever documents `Crawl-delay: 1`; everything else is
#: undocumented and capped conservatively (§5.12).
DEFAULT_RATE = 2.0
HOST_RATE_LIMITS: dict[str, float] = {
    "api.lever.co": 1.0,
    "api.eu.lever.co": 1.0,
    "jobs.lever.co": 1.0,
    "jobs.eu.lever.co": 1.0,
}

MAX_RETRIES = 3  # 429 and 5xx
SOFT_RETRIES = 1  # timeout and malformed body
BACKOFF_BASE = 1.0  # 1 s -> 2 s -> 4 s
JITTER = 0.25


class FetchError(Exception):
    """Base for every fetch failure. ``status`` is the §5.12 summary status verbatim."""

    status = "http_error"

    def __init__(self, message: str, *, url: str | None = None, http_status: int | None = None):
        super().__init__(message)
        self.url = url
        self.http_status = http_status


class NotFound(FetchError):
    status = "not_found"


class RateLimited(FetchError):
    status = "rate_limited"


class HttpError(FetchError):
    status = "http_error"


class Timeout(FetchError):
    status = "timeout"


class ParseError(FetchError):
    status = "parse_error"


def backoff_delay(attempt: int, *, rng: random.Random | None = None) -> float:
    """1 s, 2 s, 4 s for attempts 1..3, each with +/-25% jitter (§5.12)."""
    base = BACKOFF_BASE * (2 ** (attempt - 1))
    jitter = (rng or random).uniform(-JITTER, JITTER)
    return max(0.0, base * (1 + jitter))


def retry_after_seconds(response: httpx.Response) -> float | None:
    """`Retry-After` in delta-seconds form. The HTTP-date form falls back to backoff."""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        return None


class TokenBucket:
    """One host's request allowance. Holding the lock across the sleep serialises the
    host, which is the point: a burst of 8 companies must not all fire at once."""

    __slots__ = ("_lock", "_rate", "_tokens", "_updated", "_now")

    def __init__(self, rate: float, *, now: Callable[[], float] = time.monotonic):
        self._rate = rate
        self._tokens = rate
        self._now = now
        self._updated = now()
        self._lock = asyncio.Lock()

    @property
    def rate(self) -> float:
        return self._rate

    async def acquire(self, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        async with self._lock:
            while True:
                now = self._now()
                self._tokens = min(self._rate, self._tokens + (now - self._updated) * self._rate)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await sleep((1.0 - self._tokens) / self._rate)


class Client:
    """`httpx.AsyncClient` plus the §5.12 rules. Adapters only ever see this."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rate_limits: dict[str, float] | None = None,
        default_rate: float = DEFAULT_RATE,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._http = http
        self._sleep = sleep
        self._rate_limits = HOST_RATE_LIMITS if rate_limits is None else rate_limits
        self._default_rate = default_rate
        self._clock = clock
        self._buckets: dict[str, TokenBucket] = {}

    async def __aenter__(self) -> Client:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    def bucket(self, url: str) -> TokenBucket:
        host = (urlsplit(url).hostname or "").lower()
        bucket = self._buckets.get(host)
        if bucket is None:
            rate = self._rate_limits.get(host, self._default_rate)
            bucket = self._buckets[host] = TokenBucket(rate, now=self._clock)
        return bucket

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = False,
        retries: int = MAX_RETRIES,
    ) -> httpx.Response:
        """GET with the §5.12 retry table applied. Raises a :class:`FetchError` subclass.

        Redirects are **not** followed by default and a 3xx is `not_found`: Personio
        answers an unknown company with 307 and Recruitee with 301 to
        `careers_not_hosted` (§5.6, §5.8, §6.3), so following them would turn a missing
        board into a 200 page of marketing HTML. Pass ``follow_redirects=True`` where an
        adapter genuinely wants the target.
        """
        hard = soft = 0
        while True:
            await self.bucket(url).acquire(self._sleep)
            try:
                response = await self._http.get(
                    url, params=params, headers=headers, follow_redirects=follow_redirects
                )
            except httpx.TimeoutException as exc:
                if soft < SOFT_RETRIES:
                    soft += 1
                    await self._sleep(backoff_delay(soft))
                    continue
                raise Timeout(f"timeout after {soft} retries: {url}", url=url) from exc
            except httpx.HTTPError as exc:
                if hard < retries:
                    hard += 1
                    await self._sleep(backoff_delay(hard))
                    continue
                raise HttpError(f"{type(exc).__name__}: {exc}", url=url) from exc

            code = response.status_code
            if code == 429:
                if hard < retries:
                    hard += 1
                    wait = retry_after_seconds(response)
                    await self._sleep(backoff_delay(hard) if wait is None else wait)
                    continue
                raise RateLimited(f"429 after {hard} retries: {url}", url=url, http_status=code)
            if code >= 500:
                if hard < retries:
                    hard += 1
                    await self._sleep(backoff_delay(hard))
                    continue
                raise HttpError(f"{code} after {hard} retries: {url}", url=url, http_status=code)
            if code == 404 or (300 <= code < 400 and not follow_redirects):
                raise NotFound(f"{code}: {url}", url=url, http_status=code)
            if code >= 400:
                raise HttpError(f"{code}: {url}", url=url, http_status=code)
            return response

    async def get_json(
        self,
        url: str,
        *,
        parse: Callable[[httpx.Response], Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """GET and decode. One retry on a malformed or truncated body (§5.12).

        ``parse`` lets the Personio adapter hand in its defusedxml parser and inherit the
        same retry rule; the default is `response.json()`.
        """
        soft = 0
        while True:
            response = await self.get(url, **kwargs)
            try:
                return parse(response) if parse else response.json()
            except Exception as exc:
                if soft < SOFT_RETRIES:
                    soft += 1
                    await self._sleep(backoff_delay(soft))
                    continue
                raise ParseError(f"unparseable body: {url} ({exc})", url=url) from exc


def make_client(
    *,
    timeout_secs: float = 30.0,
    max_connections: int = 32,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rate_limits: dict[str, float] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Client:
    """The one place an HTTP client is built. `requestTimeoutSecs` maps straight in."""
    http = httpx.AsyncClient(
        headers=DEFAULT_HEADERS,
        timeout=httpx.Timeout(timeout_secs),
        follow_redirects=False,
        limits=httpx.Limits(max_connections=max_connections),
        transport=transport,
    )
    return Client(http, sleep=sleep, rate_limits=rate_limits, clock=clock)
