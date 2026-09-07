"""``python -m src`` entrypoint — same run as ``python -m src.main`` (§9.2)."""

from __future__ import annotations

import asyncio

from .main import main

if __name__ == "__main__":
    asyncio.run(main())
