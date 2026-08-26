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

__all__ = ["DISABLED", "PROVIDERS", "AdapterNotFound", "get_adapter"]


class AdapterNotFound(LookupError):
    """Raised when a provider has no importable adapter module in this build."""


#: §14.3 step 2 / §15.1 policy 4 — the takedown switch. Add a provider name here and
#: collection stops on the next run: `process_company` already turns `AdapterNotFound`
#: into a free `provider_unavailable` error row, so the run still succeeds and still bills
#: nothing for it. Honouring a takedown inside the 48 hours §15.1 promises otherwise meant
#: deleting a module and rebuilding, which is not a thing anyone does under time pressure
#: (V1 M6). Kept as a constant rather than an input: this is our policy lever, not the
#: buyer's.
DISABLED: frozenset[str] = frozenset()


def get_adapter(name: str) -> ModuleType:
    """Return the adapter module for ``name`` (``"greenhouse"`` -> ``core.providers.greenhouse``).

    Raises :class:`AdapterNotFound` for an unknown provider, for one disabled by
    :data:`DISABLED`, and for a known provider whose module is missing or fails to import.
    """
    if name in DISABLED:
        raise AdapterNotFound(f"provider {name!r} is disabled pending review")
    if name not in PROVIDERS:
        raise AdapterNotFound(f"unknown provider {name!r}; supported: {', '.join(PROVIDERS)}")
    try:
        module = importlib.import_module(f"core.providers.{name}")
    except ImportError as exc:  # missing module, or a broken import inside it
        raise AdapterNotFound(f"no adapter for provider {name!r} in this build: {exc}") from exc
    if not hasattr(module, "fetch"):
        raise AdapterNotFound(f"core.providers.{name} defines no fetch()")
    return module
