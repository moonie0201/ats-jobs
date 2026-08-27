"""``python -m src`` entrypoint — same pinned run as ``python -m src.main`` (§9.2)."""

from __future__ import annotations

import asyncio

from .main import PROVIDER, main

if __name__ == "__main__":
    asyncio.run(main(PROVIDER))
