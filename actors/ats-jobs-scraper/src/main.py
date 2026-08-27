"""Actor entrypoint for `ats-jobs-scraper` — the multi-ATS listing (SPEC v2 §3, §9.1).

The pipeline lives in :mod:`core.run`, which every listing in `actors/` shares. This file
exists only so the image has a `python -m src.main` to start. No provider pin: all six.
"""

from __future__ import annotations

import asyncio

from core.run import main

if __name__ == "__main__":
    asyncio.run(main())
