"""The public ATS company directory: lazy fetch, cache, lookup (SPEC v2 §6.4, §6.6).

Loaded **only** when an input entry is a bare token or a domain (§6.6) — a run made
entirely of URLs and prefixed slugs never downloads it, which is why `run.py` asks
:func:`core.resolve.needs_directory` first.

Every source is optional. A missing directory degrades to "bare tokens cannot be
resolved" (one error row each, §5.11 rule 3-4); it never fails the run.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from core.models import Ref

logger = logging.getLogger(__name__)

OWNER = os.environ.get("ATS_DIRECTORY_OWNER", "ats-jobs")
COMMIT = os.environ.get("ATS_DIRECTORY_COMMIT", "main")

JSDELIVR_URL = "https://cdn.jsdelivr.net/gh/{owner}/ats-directory@{commit}/companies.jsonl.gz"
RAW_URL = "https://raw.githubusercontent.com/{owner}/ats-directory/main/companies.jsonl.gz"
KV_STORE_NAME = "ats-directory"
KV_KEY = "companies"
BAKED_PATH = Path(__file__).parent / "data" / "companies.seed.jsonl.gz"

GZIP_MAGIC = b"\x1f\x8b"


def parse_jsonl(blob: bytes) -> list[dict[str, Any]]:
    """companies.jsonl(.gz) -> rows. A corrupt line is skipped, not fatal."""
    if blob[:2] == GZIP_MAGIC:
        blob = gzip.decompress(blob)
    rows: list[dict[str, Any]] = []
    for line in blob.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("provider") and row.get("slug"):
            rows.append(row)
    return rows


class Directory:
    """Read-only index over `companies.jsonl`.

    Lookup keys are casefolded; `slug` is stored **verbatim as it validated**, because
    Lever site names are case-sensitive (§6.4, §5.11).
    """

    __slots__ = ("rows", "source", "_by_slug", "_by_name", "_by_domain")

    def __init__(self, rows: Iterable[dict[str, Any]] = (), *, source: str = "none"):
        self.rows = [row for row in rows if row.get("status", "ok") == "ok"]
        self.source = source
        self._by_slug: dict[str, list[dict[str, Any]]] = {}
        self._by_name: dict[str, list[dict[str, Any]]] = {}
        self._by_domain: dict[str, list[dict[str, Any]]] = {}
        for row in self.rows:
            self._by_slug.setdefault(str(row["slug"]).casefold(), []).append(row)
            name_norm = row.get("name_norm") or row.get("name")
            if name_norm:
                self._by_name.setdefault(str(name_norm).casefold(), []).append(row)
            domain = row.get("domain")
            if domain:
                self._by_domain.setdefault(str(domain).casefold().removeprefix("www."), []).append(
                    row
                )

    def __len__(self) -> int:
        return len(self.rows)

    def __bool__(self) -> bool:
        return bool(self.rows)

    @staticmethod
    def _to_refs(rows: Iterable[dict[str, Any]], providers: Sequence[str] | None) -> list[Ref]:
        allowed = {p.lower() for p in providers} if providers else None
        refs: list[Ref] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            provider = str(row["provider"]).lower()
            if allowed is not None and provider not in allowed:
                continue
            key = (provider, str(row["slug"]))
            if key in seen:
                continue
            seen.add(key)
            refs.append(
                Ref(
                    provider=provider,
                    slug=str(row["slug"]),
                    site=row.get("site"),
                    region=row.get("region"),
                )
            )
        return refs

    def lookup(self, token: str, providers: Sequence[str] | None = None) -> list[Ref]:
        """§5.11 rule 3: by `slug` and by `name_norm`, restricted to `providers`."""
        key = token.strip().casefold()
        return self._to_refs([*self._by_slug.get(key, []), *self._by_name.get(key, [])], providers)

    def lookup_domain(self, domain: str, providers: Sequence[str] | None = None) -> list[Ref]:
        """§5.11 rule 4."""
        key = domain.strip().casefold().removeprefix("www.")
        return self._to_refs(self._by_domain.get(key, []), providers)


async def _from_http(client: Any, url: str) -> bytes | None:
    try:
        response = await client.get(url, follow_redirects=True)
    except Exception as exc:
        logger.info("directory source failed (%s): %s", url, exc)
        return None
    return response.content or None


async def _from_kv(opener: Any) -> bytes | None:
    try:
        store = await opener(name=KV_STORE_NAME)
        value = await store.get_value(KV_KEY)
    except Exception as exc:
        logger.info("directory KV store unavailable: %s", exc)
        return None
    if value is None:
        return None
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, list):
        return "\n".join(json.dumps(row) for row in value).encode("utf-8")
    return bytes(value)


def _from_disk(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


async def _default_kv_opener(*, name: str) -> Any:
    from apify import Actor

    return await Actor.open_key_value_store(name=name)


async def load_directory(
    client: Any | None = None,
    *,
    owner: str = OWNER,
    commit: str = COMMIT,
    kv_opener: Any | None = None,
    baked_path: Path = BAKED_PATH,
    warnings: list[str] | None = None,
) -> Directory:
    """First success wins, in §6.6 order. Returns an empty Directory if all four miss."""
    sources: list[tuple[str, Any]] = []
    if client is not None:
        sources.append(("jsdelivr", JSDELIVR_URL.format(owner=owner, commit=commit)))
        sources.append(("raw.githubusercontent", RAW_URL.format(owner=owner)))

    for name, url in sources:
        blob = await _from_http(client, url)
        rows = parse_jsonl(blob) if blob else []
        if rows:
            return Directory(rows, source=name)

    opener = kv_opener if kv_opener is not None else _default_kv_opener
    blob = await _from_kv(opener)
    rows = parse_jsonl(blob) if blob else []
    if rows:
        return Directory(rows, source="kv")

    blob = _from_disk(baked_path)
    rows = parse_jsonl(blob) if blob else []
    if rows:
        return Directory(rows, source="baked")

    logger.warning("company directory unavailable; bare slugs and domains cannot resolve")
    if warnings is not None:
        warnings.append("directory_unavailable")
    return Directory(source="none")


_cache: Directory | None = None


async def get_directory(client: Any | None = None, **kwargs: Any) -> Directory:
    """Process-wide memo, so a 2,000-entry input downloads the directory once."""
    global _cache
    if _cache is None:
        _cache = await load_directory(client, **kwargs)
    return _cache


def reset_cache() -> None:
    global _cache
    _cache = None
