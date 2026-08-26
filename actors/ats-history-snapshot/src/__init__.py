"""Actor shell for `ats-history-snapshot` (SPEC v2 §7). Private, scheduled, sells nothing.

All logic lives in ``core``; this package reads the shard input, drives the sweep and
writes the named `ats-history` key-value store. ``python -m src.main`` and ``python -m
src`` both start it (§9.2 Dockerfile CMD).
"""
