"""Shared logic for the ats-jobs Actors (SPEC v2 §9).

Every Actor under ``actors/`` is a thin ``src/main.py`` that calls into this package;
nothing ATS-specific lives outside it. Submodules are imported directly
(``from core.models import JobRecord``) so importing ``core`` stays free of side effects.
"""

__version__ = "0.1.0"
