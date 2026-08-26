#!/usr/bin/env python3
"""Schema lint for every Actor's `.actor/*.json` (SPEC v2 §11.1).

Checks, in the order §11.1 lists them:

* every `.actor/*.json` parses with `json.load`
* the input schema is under 500 kB
* no property is both `required` and carries a `default` (Apify input schema v1 rule)
* every field carries `title` + `description` (+ `example` on dataset fields), and every
  description is under 500 chars (MCP truncates at 500)
* the word "Official" appears in no title, name or other SEO-visible field (§3.3, V1 L6)
* the provider enum is identical across `input_schema.json`, `dataset_schema.json` and
  the Actor README — a drifting enum is the Congruency failure §13.5 warns about
* `actor.json`'s description names every provider in the input enum and keeps the
  "no scraping / no proxies / no API keys" clause the Store listing sells on — H2 P3-1:
  the Store description and `actor.json` had already diverged, and only the Store one is
  visible, so the drift was silent
* every `core.models.STATUSES` value is named in the dataset schema's `status`
  description: the Actor pushed `provider_unavailable` while the schema documented seven
  of the twelve statuses, and the provider-only lint never saw it (V1 M2)

Exits 1 and prints every problem it found; exits 0 silently-ish on success.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.models import STATUSES  # noqa: E402

ACTORS_DIR = ROOT / "actors"

MAX_INPUT_SCHEMA_BYTES = 500_000
MAX_DESCRIPTION_CHARS = 500
#: §4.1: "enum lists short (MCP combines enums to <=2000 chars)". The combined budget is
#: what MCP actually spends, so summing every enum in the document is the check, not
#: capping each one on its own (V1 L11).
MAX_COMBINED_ENUM_CHARS = 2000
BANNED = re.compile(r"\bofficial\b", re.IGNORECASE)
# Fields a buyer or a search engine sees as the product's own name.
SEO_KEYS = frozenset({"title", "name", "enumTitles", "sectionCaption", "label", "buildTag"})


def _walk(node: Any, path: str = "") -> list[tuple[str, str, Any]]:
    """Yield (path, key, value) for every mapping entry, depth-first."""
    out: list[tuple[str, str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else key
            out.append((here, key, value))
            out.extend(_walk(value, here))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            out.extend(_walk(value, f"{path}[{i}]"))
    return out


def check_no_official(doc: Any, label: str, errors: list[str]) -> None:
    for path, key, value in _walk(doc, label):
        if key not in SEO_KEYS:
            continue
        text = " ".join(value) if isinstance(value, list) else value
        if isinstance(text, str) and BANNED.search(text):
            errors.append(f"{path}: 'Official' is banned from titles/names/SEO fields (§3.3)")


def check_field_docs(
    properties: dict[str, Any],
    label: str,
    errors: list[str],
    *,
    keys: tuple[str, ...] = ("title", "description"),
) -> None:
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            errors.append(f"{label}.{name}: expected an object")
            continue
        for required_key in keys:
            # Presence, not truthiness: `"example": 0` and `"example": false` are real.
            if required_key not in spec or spec[required_key] in (None, ""):
                errors.append(f"{label}.{name}: missing '{required_key}'")
        description = spec.get("description", "")
        if len(description) >= MAX_DESCRIPTION_CHARS:
            errors.append(
                f"{label}.{name}: description is {len(description)} chars, "
                f"must stay under {MAX_DESCRIPTION_CHARS} (MCP truncates)"
            )


def _enums(node: Any) -> list[list[Any]]:
    """Every `enum` list in the document, at any depth."""
    if isinstance(node, dict):
        found = [node["enum"]] if isinstance(node.get("enum"), list) else []
        return found + [e for value in node.values() for e in _enums(value)]
    if isinstance(node, list):
        return [e for item in node for e in _enums(item)]
    return []


def check_input_schema(path: Path, doc: dict[str, Any], errors: list[str]) -> list[str]:
    """Returns the provider enum so the caller can compare it against the other files."""
    label = path.name
    size = path.stat().st_size
    if size > MAX_INPUT_SCHEMA_BYTES:
        errors.append(f"{label}: {size} bytes, over the {MAX_INPUT_SCHEMA_BYTES} byte limit")

    properties = doc.get("properties", {})
    check_field_docs(properties, label, errors)

    enum_chars = sum(len(json.dumps(enum)) for enum in _enums(doc))
    if enum_chars >= MAX_COMBINED_ENUM_CHARS:
        errors.append(
            f"{label}: enum lists total {enum_chars} chars, must stay under "
            f"{MAX_COMBINED_ENUM_CHARS} (MCP combines them)"
        )

    for name in doc.get("required", []):
        if name not in properties:
            errors.append(f"{label}: required field '{name}' is not in properties")
        elif "default" in properties[name]:
            errors.append(f"{label}.{name}: required fields must use 'prefill', never 'default'")

    return properties.get("providers", {}).get("items", {}).get("enum", [])


def check_dataset_schema(path: Path, doc: dict[str, Any], errors: list[str]) -> list[str]:
    label = path.name
    properties = doc.get("fields", {}).get("properties", {})
    if not properties:
        errors.append(f"{label}: no fields.properties")
        return []
    # §4.2: "title/description/example are present on every field because that is what
    # MCP agents read" — the lint checked two of the three (V1 L6).
    check_field_docs(properties, label, errors, keys=("title", "description", "example"))

    status_text = properties.get("status", {}).get("description", "")
    named = set(re.findall(r"[a-z_]+", status_text))
    missing = [value for value in STATUSES if value not in named]
    if missing:
        errors.append(
            f"{label}.status: description never names {missing}, "
            "which core.models.STATUSES declares"
        )

    for view_name, view in doc.get("views", {}).items():
        for field_name in view.get("transformation", {}).get("fields", []):
            if field_name not in properties:
                errors.append(
                    f"{label}: view '{view_name}' shows '{field_name}', "
                    "which is not a declared field"
                )

    enum = properties.get("provider", {}).get("enum", [])
    return [value for value in enum if value is not None]


#: H2 P3-1. The Store description is edited in Console and `actor.json`'s is edited here;
#: the page shows the Store one, so a divergence is invisible until a buyer reads both.
CONGRUENT_CLAUSES = ("no scraping", "no proxies", "no api keys")


def check_actor_description(
    doc: dict[str, Any], label: str, providers: list[str], errors: list[str]
) -> None:
    text = str(doc.get("description") or "").casefold()
    if not text:
        errors.append(f"{label}: no description")
        return
    missing = [p for p in providers if p.casefold() not in text]
    if missing:
        errors.append(f"{label}: description never names {missing} (§13.5 congruency)")
    absent = [c for c in CONGRUENT_CLAUSES if c not in text]
    if absent:
        errors.append(f"{label}: description drops the Store listing's {absent} clause")


def check_readme_providers(readme: Path, providers: list[str], errors: list[str]) -> None:
    text = readme.read_text(encoding="utf-8").casefold()
    missing = [p for p in providers if p.casefold() not in text]
    if missing:
        errors.append(
            f"{readme.name}: input schema lists {missing}, the README never mentions them"
        )
    if BANNED.search(text.split("\n", 1)[0]):
        errors.append(f"{readme.name}: 'Official' in the H1 (§3.3)")


def validate_actor(actor_dir: Path, errors: list[str]) -> None:
    dot_actor = actor_dir / ".actor"
    docs: dict[str, Any] = {}
    for path in sorted(dot_actor.glob("*.json")):
        try:
            docs[path.name] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{actor_dir.name}/{path.name}: invalid JSON — {exc}")
    if not docs:
        errors.append(f"{actor_dir.name}: no .actor/*.json files")
        return

    for name, doc in docs.items():
        check_no_official(doc, f"{actor_dir.name}/{name}", errors)

    actor_json = docs.get("actor.json")
    if actor_json is None:
        errors.append(f"{actor_dir.name}: no .actor/actor.json")
    else:
        referenced = [
            actor_json.get("dockerfile"),
            actor_json.get("input"),
            actor_json.get("output"),
            actor_json.get("storages", {}).get("dataset"),
        ]
        for rel in referenced:
            if rel and not (dot_actor / rel).exists():
                errors.append(f"{actor_dir.name}/actor.json references missing file '{rel}'")

    input_enum: list[str] = []
    if "input_schema.json" in docs:
        input_enum = check_input_schema(
            dot_actor / "input_schema.json", docs["input_schema.json"], errors
        )
    dataset_enum: list[str] = []
    if "dataset_schema.json" in docs:
        dataset_enum = check_dataset_schema(
            dot_actor / "dataset_schema.json", docs["dataset_schema.json"], errors
        )

    if input_enum and dataset_enum and input_enum != dataset_enum:
        errors.append(
            f"{actor_dir.name}: provider enum drift — input_schema {input_enum} "
            f"vs dataset_schema {dataset_enum}"
        )

    readme = actor_dir / "README.md"
    if input_enum and readme.exists():
        check_readme_providers(readme, input_enum, errors)
    if input_enum and actor_json is not None:
        check_actor_description(actor_json, f"{actor_dir.name}/actor.json", input_enum, errors)


def main() -> int:
    actor_dirs = sorted(d for d in ACTORS_DIR.glob("*") if (d / ".actor").is_dir())
    if not actor_dirs:
        print(f"no Actors with a .actor/ directory under {ACTORS_DIR}", file=sys.stderr)
        return 1

    errors: list[str] = []
    for actor_dir in actor_dirs:
        validate_actor(actor_dir, errors)

    for error in errors:
        print(f"ERROR {error}", file=sys.stderr)
    if errors:
        print(f"{len(errors)} schema problem(s)", file=sys.stderr)
        return 1
    print(f"schemas OK: {', '.join(d.name for d in actor_dirs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
