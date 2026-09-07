"""Actor entrypoint for `ats-jobs-lifecycle` (SPEC v2 §3, §9.1).

The pipeline lives in :mod:`core.enrich_run`. This file exists only so the image has a
`python -m src.main` to start.
"""

from __future__ import annotations

import asyncio

from core.enrich_run import main

if __name__ == "__main__":
    asyncio.run(main())
