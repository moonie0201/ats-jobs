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
| `tests/` | offline unit tests over committed provider payloads in `tests/fixtures/` |
| `scripts/` | fixture refresh, fixture scrub, actor file sync, schema lint, directory build, launch KPIs |

Specification: `spec/SPEC_v2.md` — it is the authority, this file is a signpost. It is not
in this repository and is not published.

## Test fixtures carry no employer prose

`tests/fixtures/` holds captures from the six vendors' public job-board APIs. The
advertisement bodies in them are replaced with synthetic filler by
`scripts/scrub_fixtures.py`, which preserves every tag, entity, CDATA wrapper and salary
figure and throws the words away — the parsers are tested on shape, never on copy. A job ad
is the employer's copyrighted work and this repository is public, so the raw text has no
business being in it. `scripts/refresh_fixtures.py` runs the scrub itself after every
download, and CI fails if any fixture arrives unscrubbed.

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

## Launch KPIs

`scripts/kpi.py` prints the D14 scoreboard for the published Actor
(`acotr_moonie/ats-jobs-scraper`) and the GO/HOLD/KILL verdict from `spec/SPEC_v2.md`
§16.2. Store-wide stats come from the public Actor object; platform cost and the
charged-event shape come from the developer's own runs.

```bash
python scripts/kpi.py             # KPI table + verdict
python scripts/kpi.py --selftest  # verdict truth table, no network
ATS_D0=2026-08-26 python scripts/kpi.py   # override the D0 the countdown uses
```

Auth is `APIFY_TOKEN` if set, otherwise the logged-in `apify` CLI — the CLI keeps its
token in the OS keyring, and the script never reads the secret itself.

Rows prefixed `~` are **derived, not measured**: an Actor developer cannot enumerate
other accounts' runs, inputs or charged events, so revenue is extrapolated from the
median jobs-per-run of our own runs (§16.2, V3 M3). Treat them as a shape, not a
number.

The launch copy those KPIs are measuring — channel-by-channel posts and the day-by-day
checklist — lives in `spec/LAUNCH.md`.

## Removal, takedown and privacy

[`TAKEDOWN.md`](TAKEDOWN.md) — copyright, provider and directory removal. 48 hours, no
argument, no justification asked for.
[`PRIVACY.md`](PRIVACY.md) — what is held, what is not, and unconditional erasure and
objection.

## License

MIT for the source code — see [`LICENSE`](LICENSE), including the scope note that says what
the MIT grant does **not** cover (`tests/fixtures/`, `core/data/companies.seed.jsonl.gz`).

## Disclaimer

Unofficial. Not affiliated with, sponsored by or endorsed by Greenhouse, Lever, Ashby,
Recruitee, Rippling, Personio, Apify or any other applicant tracking system. All product
names and trademarks belong to their respective owners. Job advertisement text remains the
copyright of the employer who wrote it.
