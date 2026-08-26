"""Deterministic, offline normalization rules (SPEC v2 §4.5).

No LLM, no network, no geocoding. Every rule that cannot decide emits ``None`` rather
than a guess, and every inferred field exports its provenance in a ``*Source`` sibling.

Submodules are imported directly (``from core.normalize.location import parse_location``).
"""
