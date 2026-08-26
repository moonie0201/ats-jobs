#!/usr/bin/env python3
"""Copy the shared build inputs into an Actor directory (SPEC v2 §9.2, §11.2).

`apify push` builds with `working-directory: actors/<name>`, so that directory *is* the
Docker build context. `Dockerfile` says `COPY core ./core` and `COPY requirements.txt ./`
— neither of which lives there in git, so the image could not build at all (V1 B2). This
script puts them where the build expects them, right before the push.

The copies are build artefacts, not a second source of truth: `.gitignore` keeps
`actors/*/core/` and `actors/*/requirements.txt` out of the repo, and this script
replaces them wholesale every time.

    python scripts/sync_actor_files.py ats-jobs-scraper     # one Actor
    python scripts/sync_actor_files.py                      # every Actor
    python scripts/sync_actor_files.py --clean              # remove the copies again

Run it right before `apify push` / `docker build`, and `--clean` afterwards: with a
`core/` package sitting beside `src/`, a local `apify run` cannot tell which package is
the entrypoint and refuses to start.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ACTORS_DIR = ROOT / "actors"

#: Directories never worth shipping into a build context (V3 S16 — no `.dockerignore`
#: existed, so `COPY core ./core` shipped every `__pycache__`).
IGNORE = shutil.ignore_patterns("__pycache__", "*.py[cod]", "*.egg-info")


def sync(actor_dir: Path) -> list[Path]:
    """Refresh `core/` and `requirements.txt` inside one Actor. Returns what it wrote."""
    written: list[Path] = []

    target_core = actor_dir / "core"
    if target_core.exists():
        shutil.rmtree(target_core)
    shutil.copytree(ROOT / "core", target_core, ignore=IGNORE)
    written.append(target_core)

    target_requirements = actor_dir / "requirements.txt"
    header = (
        "# Synced copy of the repo-root requirements.txt "
        "(scripts/sync_actor_files.py, SPEC v2 §9.2).\n"
        "# Edit the root file, not this one.\n"
    )
    target_requirements.write_text(
        header + (ROOT / "requirements.txt").read_text(encoding="utf-8"), encoding="utf-8"
    )
    written.append(target_requirements)
    return written


def clean(actor_dir: Path) -> list[Path]:
    """Remove what :func:`sync` wrote. A `core/` beside `src/` breaks `apify run`."""
    removed: list[Path] = []
    target_core = actor_dir / "core"
    if target_core.exists():
        shutil.rmtree(target_core)
        removed.append(target_core)
    # V1 L12: `sync` writes this too, so `--clean` has to take it back — otherwise the
    # docstring above is false and the tree is not returned to its pre-sync state.
    target_requirements = actor_dir / "requirements.txt"
    if target_requirements.exists():
        target_requirements.unlink()
        removed.append(target_requirements)
    return removed


def main(argv: list[str]) -> int:
    cleaning = "--clean" in argv
    argv = [arg for arg in argv if not arg.startswith("-")]
    names = argv or [d.name for d in sorted(ACTORS_DIR.glob("*")) if (d / ".actor").is_dir()]
    if not names:
        print(f"no Actors under {ACTORS_DIR}", file=sys.stderr)
        return 1
    for name in names:
        actor_dir = ACTORS_DIR / name
        if not (actor_dir / ".actor").is_dir():
            print(f"no such Actor: {name}", file=sys.stderr)
            return 1
        if cleaning:
            for path in clean(actor_dir):
                print(f"removed {path.relative_to(ROOT)}")
            continue
        for path in sync(actor_dir):
            print(f"synced {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
