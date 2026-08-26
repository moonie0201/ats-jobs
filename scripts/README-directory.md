# Company directory builder

`scripts/build_directory.py` builds the public ATS company directory: SPEC v2 §6.2
(sources), §6.3 (validation), §6.4 (record format), §6.6 (storage and read path).

```bash
python scripts/build_directory.py --selfcheck        # offline asserts, no network
python scripts/build_directory.py --cap 400          # the M1 seed run
python scripts/build_directory.py --cap 50 --no-wayback --dry-run    # fast smoke
```

## What it writes

| Path | Contents | Why |
|---|---|---|
| `core/data/companies.seed.jsonl.gz` | the full §6.4 record | baked into the Docker image; §6.6 read-path source 4 |
| `../ats-directory/companies.jsonl` | the §6.6 public projection | the public repo, human-readable for PRs |
| `../ats-directory/companies.jsonl.gz` | the same rows gzipped | what §6.6 read-path sources 1–2 actually fetch from jsDelivr / raw.githubusercontent |

Rows are sorted by `(provider, casefold(slug))` so a rebuild produces a minimal diff, and
gzip is written with `mtime=0` so an unchanged directory produces byte-identical output —
a blob that reshuffles every night defeats the commit-pinned, year-long jsDelivr cache
§6.6 relies on.

## Sources (§6.2)

| Rank | Source | Endpoint | Cadence |
|---|---|---|---|
| 0 | **SimplifyJobs** | `raw.githubusercontent.com/SimplifyJobs/{Summer2027-Internships,New-Grad-Positions}/dev/.github/scripts/listings.json` | daily |
| 1 | **HN "Who is hiring"** | `hn.algolia.com/api/v1/search?tags=story,author_whoishiring` → `tags=comment,story_{id}` | monthly |
| 2 | **Wayback CDX** | `web.archive.org/cdx/search/cdx?url={host}/{p}*&fl=original&collapse=urlkey&filter=original:{root-regex}` | monthly |

Rank is the priority order for the `--cap`: a slug carried by a live job feed today is a
better use of a validation probe than an arbitrary decade-old capture. Wayback runs only
to top up providers the cheap sources left short of the cap.

SimplifyJobs' `company_name` and `company_url` are read **for prioritisation only and
never republished** (§6.2, §15.3). The script parses the `url` field and nothing else; the
`name` on a published row always comes from the provider's own API during validation.

### Common Crawl is not a source, by design

The task this script was built from asked for Common Crawl CDX index queries. **§6.2
removed Common Crawl from the pipeline entirely (V1 L2)** and the spec is the authority:

- `index.commoncrawl.org/robots.txt` is `Disallow: /` with an `Allow` list covering only
  `/$`, `/index.html$`, `/web-graphs-index.html$`, `/collinfo.json$`, `/graphinfo.json$`,
  `/ccbot.json$` and `/.well-known/*.txt$`. `/{CRAWL}-index?url=…` — the CDX query itself —
  is not on that list.
- `data.commoncrawl.org/robots.txt` disallows everything.
- The CC ToU grant is *non-sublicensable*, which is incompatible with re-publishing derived
  rows under CC0, and CC "strongly recommends … legal counsel before making any use,
  including commercial use".

`cc_cdx.py` and `cc_hostgraph.py` were deleted for exactly this reason. If Common Crawl is
ever judged worth keeping, §6.2's stated route is bulk S3 access under CC's own documented
path plus a mail to CC — not the index server, and not before M4.

### Wayback query shape

Two shapes, because slugs live in two places:

- **Path-carried** (Greenhouse, Lever, Ashby, Rippling): `url={host}/{p}*` with a root-URL
  `filter`. Sliced per first character because the CDX server truncates a long scan — an
  unsliced `boards.greenhouse.io/*` root query returns partial results after ~90 s and
  stops around the letter *d*, which would produce an alphabetically biased directory.
  `jobs.lever.co` returns an incomplete chunked body outright at that size.
- **Subdomain-carried** (Recruitee, Personio): `matchType=domain` with a subdomain filter.
  These cannot be sliced by subdomain prefix and do not need to be — one capture per
  company is the norm, so a 40,000-row query answers in ~4 s.

URL → `(provider, slug, region)` extraction reuses `core.resolve.parse_url`, the Actor's
own §5.11 parser, rather than a second set of host regexes. That is what keeps discovery
and the shipped Actor agreeing on what a slug is, including the V3 S1 charset gate and the
`jobs.eu.lever.co` → `region="eu"` rule.

## Validation (§6.3)

One request per candidate, per §5's endpoints, `10` concurrent overall, `1` rps per host,
same User-Agent as the Actor.

| Provider | Request | `ok` | `dead` |
|---|---|---|---|
| Greenhouse | `GET /v1/boards/{slug}` then `/jobs` | 200 | 404 |
| Lever | `GET /v0/postings/{slug}?mode=json`, US then EU (EU first when the candidate came from an EU URL) | 200 **and a non-empty array on at least one host** | 404 on both |
| Ashby | `GET /posting-api/job-board/{slug}?includeCompensation=true` | 200 | 404 |
| Recruitee | `GET https://{slug}.recruitee.com/api/offers/` | 200 | 404, or 301 to `careers_not_hosted` |
| Rippling | `GET /platform/api/ats/v1/board/{slug}/jobs` | 200 | 404 |
| Personio | `GET https://{slug}.jobs.personio.{de,com}/xml?language=en` | 200 `<workzag-jobs>` | 307/404 on both |

**Lever's 200-with-empty rule matters** (V2 T-H6): `[]` on *both* hosts is `unconfirmed`,
not `ok`. v1 accepted "200 (even `[]`)" and would have recorded `region: null` for every EU
tenant and cached the wrong host permanently.

`job_count` is a real count, not a response check — the Workable lesson (V2 T-B1) is that a
validation rule must assert *jobs*, not *a response*. Rippling's count is over distinct
`uuid`s, because its list endpoint returns one row per (job × location) and counting rows
would publish a `job_count` roughly 2× the truth (§5.7). Ashby's applies §5.3's
`isListed == false` filter so the count matches what the Actor emits.

Personio XML is parsed with `defusedxml`, never stdlib `xml.etree` — the body is
third-party (§5.8, billion-laughs / external-entity defence).

A transport failure is `unconfirmed`, never `dead`: the slug may well be alive. Only a
404/307 is a `dead` verdict.

### Provider-degradation guard (§5.12)

The report flags a provider `DEGRADED` when more than 90% of its `ok` boards return zero
jobs (minimum 10 `ok` rows before the ratio means anything), and the script exits `1`. That
is the exact signature Workable presents: a provider whose boards return `[]`
population-wide is broken, not empty.

## Record format (§6.4) and the §6.6 privacy projection

Primary key `(provider, slug)`, slug stored **verbatim as it validated**, index keyed on
the casefolded form — Lever is the one case-sensitive provider (`Palantir` 404s where
`palantir` works). Two spellings of one slug are therefore one candidate, and the
better-ranked source supplies the spelling that gets probed.

The baked seed carries the full §6.4 record:

```json
{"provider":"greenhouse","slug":"anthropic","site":null,"region":null,"name":"Anthropic","name_norm":"anthropic","domain":null,"status":"ok","job_count":546,"first_seen":"2026-08-26","checked_at":"2026-08-26","dead_since":null}
```

The public repo gets only `provider, slug, site, region, name, domain, status` — §6.6:
*"The public repo publishes only (provider, slug, site, region, name, domain, status) —
every field of which is the output of our own live validation probe"*, with `job_count`
history and per-company first/last-seen timelines staying in the private history store
(§7). §6.4's example row and §6.6's enumeration disagree on this point; the script follows
§6.6, because a git-tracked `job_count` and `first_seen` **is** a per-company timeline the
moment there are two commits. `core.directory.Directory` reads the public file fine — it
falls back from `name_norm` to `name` for name lookups and only requires `provider`,
`slug` and `status`.

`sources` is never written to either file (V1 L7). `domain` is null on every row today:
§6.4 permits it only from the provider's own response (none of the six APIs returns one),
yc-oss, Wikidata, or Clearbit autocomplete — and §6.2 marks Clearbit's bulk-use terms
`[unverified]`, so it is left null rather than guessed. It is nullable by design and never
blocks ingestion.

## History rules (§6.3)

- `first_seen` is preserved across rebuilds: *"Never delete history — a company that turns
  hiring off and on again must keep its `first_seen`."* It is the one field a rebuild
  cannot recover.
- `dead_since` records the **first** failure, not the latest one, and clears on recovery.
- Rows the run did not probe survive untouched. A capped run is a partial refresh, not a
  statement that everything it skipped has vanished.
- Not yet implemented here: the 90-consecutive-days-dead drop, and weekly revalidation
  cadence. Both belong to Actor B2's scheduler (§6.5), not to a seed script.

## Two HTTP clients, deliberately

Validation goes through `core.http.make_client`, so probes inherit the Actor's User-Agent,
the §5.12 retry table, the per-host token bucket, the response-size cap and the
`ALLOWED_HOST_SUFFIXES` SSRF guard.

Discovery uses a separate plain `httpx` client with its own 1 rps-per-host bucket and a
semaphore of 3. `web.archive.org` and `hn.algolia.com` are **not** on the Actor's host
allowlist and must not be added to it — the shipped Actor has no business calling either.
A build-time script reaching them is fine; widening the runtime allowlist to match would
not be.

`make_client` exposes no `default_rate` parameter, so §6.3's uniform 1 rps cap is set on
the returned instance rather than by forking the hardened constructor, which carries the
redirect-hop request hook. Buckets are built lazily on first use, so the assignment lands
before any request.

### The probe queue is interleaved, not concatenated

§5.12: Greenhouse, Lever, Ashby and Rippling are each **one shared host for every
company**; only Recruitee and Personio are genuinely per-company hosts. So four providers'
probes each serialise on a single 1 rps token bucket. Queued provider-by-provider, the
first `cap` tasks are all Greenhouse, they fill the semaphore, and the other five starve
behind a bucket they do not even share — six overlapping ~7 min queues collapse into one
~45 min chain. The queue is therefore round-robinned across providers so the four buckets
drain concurrently. This is a scheduling detail, not a politeness one: the per-host rate
is unchanged either way.

## Options

| Flag | Default | Notes |
|---|---|---|
| `--cap` | `400` | candidates probed per provider |
| `--providers` | all six | comma-separated |
| `--concurrency` | `10` | §6.3: 10 concurrent overall |
| `--rate` | `1.0` | §6.3: requests/second per host |
| `--prefixes` | `amesicpgtblr1` | Wayback slices, spread across the keyspace so an early stop still covers the alphabet |
| `--wayback-rows` | `40000` | CDX `limit` per query |
| `--no-wayback` | off | cheap sources only |
| `--dry-run` | off | probe and report, write nothing |
| `--selfcheck` | off | offline asserts, no network |

Exit codes: `0` clean, `1` a provider tripped the §5.12 degradation guard.

## Seed size targets (§6.7)

M1 wants ≥2,000 validated `status=ok` rows across Greenhouse, Lever and Ashby. The report
prints whether the target is met. A single `--cap 400` run over six providers is a seed,
not the target: reaching 2,000 means raising the cap and letting the run take hours, which
is why the cap is a flag rather than a constant.
