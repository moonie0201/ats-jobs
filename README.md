# ats-jobs

Apify Actors that read job postings from the **public job-board APIs** of Greenhouse,
Lever, Ashby, Recruitee, Rippling and Personio, and normalize them into one schema with
parsed location, remote flag and structured salary.

No HTML scraping, no login, no headless browser, no proxies — every request goes to an
endpoint the vendor publishes for exactly this purpose. Where a value cannot be
determined the field is `null`; nothing is guessed.

## Layout

| Path | What lives there |
|---|---|
| `core/` | all the logic: `adapters/`, `normalize/`, resolve, filters, dedupe, state, billing |
| `core/models.py` | `Ref`, `Meta`, `Location`, `Salary`, `JobRecord`, `ProviderSpec` |
| `actors/<name>/` | one thin Actor each: `.actor/*.json`, `src/main.py`, `Dockerfile` |
| `tests/` | offline unit tests over committed real payloads in `tests/fixtures/` |
| `scripts/` | fixture refresh, actor file sync, schema lint, smoke run |
| `legal/` | dated captures of the vendor statements the adapters rely on |

Specification: `spec/SPEC_v2.md` — it is the authority, this file is a signpost.

## Development

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

ruff check . && ruff format --check .
python -m pytest -q -m "not live"      # offline; drop the -m to hit live endpoints
python scripts/validate_schemas.py     # lints every actors/*/.actor/*.json
```

Python 3.12 is the deployment target (`apify/actor-python:3.12`); the Apify SDK v4 floor
is 3.11.

## License

MIT — see `LICENSE`.

## Disclaimer

Unofficial. Not affiliated with, sponsored by or endorsed by Greenhouse, Lever, Ashby,
Recruitee, Rippling, Personio or any other applicant tracking system. All product names
and trademarks belong to their respective owners.
