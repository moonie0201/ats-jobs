"""Provider adapter registry (SPEC v2 §5).

One module per provider, each exposing::

    async def fetch(ref: Ref, client, options: dict | None = None) -> list[JobRecord]

Adapters are imported **lazily** by name so that a build missing one provider still
starts, still serves the other five, and reports the gap as a data row rather than as a
stack trace on import (§13.4 requirement 14). The name list is
:data:`core.models.PROVIDERS` — the same tuple the input schema and the dataset schema
enumerate, so a provider can never be half-added.
"""

from __future__ import annotations

import importlib
from types import ModuleType

from core.models import PROVIDERS

__all__ = ["PROVIDERS", "AdapterNotFound", "get_adapter"]


class AdapterNotFound(LookupError):
    """Raised when a provider has no importable adapter module in this build."""


def get_adapter(name: str) -> ModuleType:
    """Return the adapter module for ``name`` (``"greenhouse"`` -> ``core.providers.greenhouse``).

    Raises :class:`AdapterNotFound` for an unknown provider and for a known provider
    whose module is missing or fails to import.
    """
    if name not in PROVIDERS:
        raise AdapterNotFound(f"unknown provider {name!r}; supported: {', '.join(PROVIDERS)}")
    try:
        module = importlib.import_module(f"core.providers.{name}")
    except ImportError as exc:  # missing module, or a broken import inside it
        raise AdapterNotFound(f"no adapter for provider {name!r} in this build: {exc}") from exc
    if not hasattr(module, "fetch"):
        raise AdapterNotFound(f"core.providers.{name} defines no fetch()")
    return module
