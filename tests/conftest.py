"""Shared test fixtures (SPEC v2 §10.1).

Every payload under ``tests/fixtures/<provider>/`` is a real response captured from the
live public endpoint and committed, refreshable with ``scripts/refresh_fixtures.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(provider: str, name: str) -> Any:
    """Read ``tests/fixtures/<provider>/<name>``.

    ``.json`` files come back parsed; ``.xml`` / ``.html`` come back as text, because the
    Personio feed and the Recruitee not-hosted page are not JSON. A bare ``name`` with no
    suffix is treated as ``.json``.
    """
    path = FIXTURES_DIR / provider / name
    if not path.suffix:
        path = path.with_suffix(".json")
    if not path.exists():
        available = (
            sorted(p.name for p in (FIXTURES_DIR / provider).glob("*"))
            if (FIXTURES_DIR / provider).is_dir()
            else []
        )
        raise FileNotFoundError(f"no fixture {provider}/{path.name}; have {available}")
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if path.suffix == ".json" else text


@pytest.fixture
def fixture():
    """`fixture("greenhouse", "anthropic.json")` -> parsed payload."""
    return load_fixture


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR
