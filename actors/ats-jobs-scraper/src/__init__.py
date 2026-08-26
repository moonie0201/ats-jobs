"""Actor shell for `ats-jobs-scraper` (SPEC v2 §4).

All logic lives in ``core``; this package only reads the Apify input, drives the
pipeline and pushes dataset rows. ``python -m src.main`` and ``python -m src`` both
start it (§9.2 Dockerfile CMD).
"""
