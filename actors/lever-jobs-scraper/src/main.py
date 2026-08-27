"""Actor entrypoint for `lever-jobs-scraper` — the Lever listing (SPEC v2 §3.2, §9.1).

A per-ATS listing is glue, not a fork: the pipeline, the adapters, the normalization, the
`job` / `delta-run` events and the price are all :mod:`core.run`'s, exactly as the
multi-ATS Actor runs them. The only difference is the pin below, which drops the provider
selector from the input schema and makes a bare token a Lever board slug.
"""

from __future__ import annotations

import asyncio

from core.run import main

PROVIDER = "lever"

if __name__ == "__main__":
    asyncio.run(main(PROVIDER))
