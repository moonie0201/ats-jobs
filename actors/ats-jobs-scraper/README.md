# ATS Jobs API — Greenhouse, Lever, Ashby +3

> **Unofficial.** Not affiliated with, endorsed by or sponsored by Greenhouse Software,
> Lever, Ashby, Recruitee, Rippling or Personio. It calls each vendor's own public
> job-board API. All trademarks belong to their owners.
> **Removal requests:** [TAKEDOWN.md](https://github.com/moonie0201/ats-jobs/blob/main/TAKEDOWN.md)
> — honoured in 48 hours. **Privacy:** [PRIVACY.md](https://github.com/moonie0201/ats-jobs/blob/main/PRIVACY.md).

Live job postings straight from six **public ATS APIs** — Greenhouse, Lever, Ashby, Recruitee, Rippling and Personio — normalized into one schema with structured salary. You give it company slugs or career-site URLs; it gives you clean job rows. Seven filters run **before** anything is billed, company summaries and error rows are free, and `onlyNewJobs` keeps a per-company baseline in your own key-value store so a monitoring run returns only what changed. No scraping, no browsers, no proxies, no API keys.

**We index nothing you buy.** There is no "jobs in our database" number here because there is no job index between you and the board — every row in your run was fetched from the employer's own board seconds earlier, never served from a crawl of unknown age. (We do keep a private daily record of *which roles were open where*, holding titles, locations and dates and no descriptions, salaries or contacts, to build hiring-history products later. It never touches your run. See [Hiring history](#hiring-history).) And because you are billed only for job rows you actually receive, a 2,000-company daily watch with `onlyNewJobs` costs about **8 cents a run**.

**What this is not:** no keyword search without a company list (directory mode ships next); `remote` is often `null` on Greenhouse, Personio and Rippling because the board never said; not an aggregator — six ATS platforms, live, and nothing else.

## What this ATS jobs API does

- **Greenhouse** — [`boards-api.greenhouse.io/v1/boards/{slug}/jobs`](https://developers.greenhouse.io/job-board.html), with pay-transparency ranges
- **Lever** — [`api.lever.co/v0/postings/{site}`](https://github.com/lever/postings-api), global and EU hosts
- **Ashby** — [`api.ashbyhq.com/posting-api/job-board/{slug}`](https://developers.ashbyhq.com/docs/public-job-posting-api), with compensation
- **Recruitee** — [`{slug}.recruitee.com/api/offers/`](https://docs.recruitee.com/reference/offers), with structured locations
- **Rippling** — [`api.rippling.com/platform/api/ats/v1/board/{slug}/jobs`](https://developer.rippling.com/documentation/job-board-api-v2), with pay ranges from the job detail
- **Personio** — [`{slug}.jobs.personio.de/xml?language=en`](https://developer.personio.de/docs/retrieving-open-job-positions), with seniority and years of experience

Each of those is the **vendor's own public job-board API**: the same endpoint the company's careers page calls to render itself. That is a different product from career-page scraping — no headless browser, no residential proxy, no bot walls, and nothing that breaks when a marketing site is restyled. It is also different from an indexed job aggregator: what you get back is what the board serves right now, not a copy from a crawl of unknown age.

Rows you are **not** charged for: company summaries, error rows, jobs removed by your filters, companies never reached because your `maxJobs` cap fired, and companies that failed.

## Supported ATS platforms and their APIs

| ATS | Endpoint used | Structured salary | Remote flag | Description | Seniority | Notes |
|---|---|---|---|---|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true&pay_transparency=true` | Yes (`pay_input_ranges`) | No | Inline | No | `url` is often the company's own careers domain; `team` and employment type are never reported |
| Lever | `api.lever.co/v0/postings/{site}?mode=json` | Sometimes (`salaryRange`) | Yes (`workplaceType`) | Inline | No | Site names are **case-sensitive**; EU boards are tried automatically |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true` | Yes (cleanest of the six) | Yes (`isRemote`) | Inline | No | Intervals arrive as `"1 YEAR"` and are normalized to `year` |
| Recruitee | `{slug}.recruitee.com/api/offers/` | Sometimes (`salary`) | Yes | Inline | No | Needs a per-company token from 2027-02-10 |
| Rippling | `api.rippling.com/platform/api/ats/v1/board/{slug}/jobs` + one detail call per job | Yes (`payRangeDetails`) | No | Detail call | No | List returns one row per job × location; we merge them by job id before you are charged |
| Personio | `{slug}.jobs.personio.de/xml?language=en` | No (regex fallback only) | No | Inline | **Yes** | Also exports `yearsOfExperience`, both provider-sourced, never inferred |

## How to use the Greenhouse, Lever and Ashby job APIs in one run

1. **Paste your companies.** One per line. Use a career-site URL (`https://job-boards.greenhouse.io/anthropic`) or a prefixed slug (`lever:palantir`, `ashby:openai`, `personio:personio`). A bare company name does not resolve yet — directory mode ships in the next release.
2. **Set your filters.** Title keywords, excluded titles, location, remote-only, departments, employment types, posted-after. They all run locally on the fetched JSON, before billing.
3. **Run it, or schedule it.** For monitoring, turn on `onlyNewJobs` and give the task its own `stateKey`; the first run stores the baseline and later runs return only what is new.

A minimal run — `["https://job-boards.greenhouse.io/anthropic", "lever:palantir"]` with `maxJobs: 50` — finishes in seconds and costs about ten cents.

## Coming from fantastic-jobs, bovi or webdata_labs

Saved input from another job-board Actor runs here unchanged. These input keys are accepted as aliases for `companies`, first non-empty wins, and an explicit `companies` always wins over an alias:

| Their key | Here |
|---|---|
| `queries`, `companyUrls`, `startUrls` | `companies` |
| `boardTokens`, `siteNames`, `jobBoardNames` | `companies` |
| `subdomains`, `companyIdentifiers` | `companies` |

**What you gain:** live boards instead of an index of unknown age; `postedAfter` and per-company filtering that run before billing; structured salary with the currency and interval separated; no contact, recruiter or candidate fields; the full ad body on by default with contact redaction on by default; and a `trackedSince` date on every company row once `onlyNewJobs` is on.

**What you lose, stated plainly:** there is no keyword search without a company list yet. Directory mode — leave `companies` empty and fan out across our public ATS directory — arrives free in the next release, and even then it searches within our directory, not the whole internet. If "find me every Rust job anywhere" is your requirement today, an aggregator is a better fit than this Actor.

## Input

```json
{
  "companies": [
    "https://job-boards.greenhouse.io/anthropic",
    "lever:palantir",
    "ashby:openai",
    "personio:personio"
  ],
  "providers": ["greenhouse", "lever", "ashby", "recruitee", "rippling", "personio"],
  "maxJobs": 1000,
  "titleKeywords": ["engineer", "data"],
  "excludeTitleKeywords": ["intern"],
  "locationKeywords": ["Berlin", "Germany", "Remote"],
  "remoteOnly": false,
  "employmentTypes": ["full_time"],
  "postedAfter": "7 days",
  "includeDescription": true,
  "descriptionFormat": "text",
  "redactContacts": true,
  "outputProfile": "full",
  "onlyNewJobs": false,
  "stateKey": "ats-jobs-state-default"
}
```

| Field | Type | Default | Effect on cost |
|---|---|---|---|
| `companies` | array | required | More companies, more jobs, more charged rows |
| `providers` | array | all six | Restricts bare slugs and directory lookups |
| `maxJobs` | integer | 1000 | **Your main cost control.** Hard stop after this many charged rows; `0` = no limit |
| `maxJobsPerCompany` | integer | 0 | Stops one enterprise board eating the whole budget |
| `titleKeywords` / `excludeTitleKeywords` | array | `[]` | Filtered-out jobs are free |
| `locationKeywords` | array | `[]` | Matches raw location text and parsed city, region, country |
| `remoteOnly` | boolean | false | Keeps only `remote: true`; unknown remote status is dropped |
| `departments` | array | `[]` | Substring match on department or team |
| `employmentTypes` | array | `[]` | Unknown types are kept unless `strictEmploymentType` is on |
| `strictEmploymentType` | boolean | false | Keeps only ATS-confirmed types |
| `postedAfter` | string | null | `2026-08-01` or `7 days`. Jobs with no date are **kept** |
| `includeDescription` | boolean | **true** | Bigger items, same price — the body costs nothing extra |
| `descriptionFormat` | string | `text` | `html`, `text` or `both` |
| `redactContacts` | boolean | true | Strips contact details from description bodies |
| `outputProfile` | string | `full` | `minimal` cuts token cost for AI agents |
| `includeCompanySummary` | boolean | true | Summary rows are always free |
| `includeRawJson` | boolean | false | Attaches the provider payload under `raw` |
| `dedupe` | string | `id` | `content` also merges same title + company + location + requisition id |
| `onlyNewJobs` | boolean | false | Returns only ids not in your state store |
| `stateKey` | string | `ats-jobs-state-default` | Name of the key-value store holding seen ids |
| `stateRetentionDays` | integer | 90 | Forgets ids that stopped appearing, so the delta store cannot grow forever |
| `maxConcurrency` | integer | 8 | Speed only; per-host rate is capped separately |
| `requestTimeoutSecs` | integer | 30 | Per-request timeout; a slow board fails its own row, not the run |
| `failOnAllErrors` | boolean | false | Fails the run when no company returned any job |

## Output

One `job` row per posting (charged), plus free `company_summary` and `error` rows.

```json
{
  "recordType": "job",
  "id": "greenhouse:anthropic:4019283",
  "provider": "greenhouse",
  "companySlug": "anthropic",
  "company": "Anthropic",
  "title": "Senior Backend Engineer, Inference",
  "department": "Engineering",
  "team": null,
  "locationRaw": "San Francisco, CA",
  "city": "San Francisco",
  "region": "CA",
  "country": "United States",
  "countryCode": "US",
  "remote": null,
  "workplaceType": null,
  "remoteSource": null,
  "employmentType": null,
  "employmentTypeSource": null,
  "salaryMin": 300000,
  "salaryMax": 405000,
  "salaryCurrency": "USD",
  "salaryInterval": "year",
  "salarySource": "ats",
  "url": "https://job-boards.greenhouse.io/anthropic/jobs/4019283",
  "postedAt": "2026-08-19T00:00:00Z",
  "postedAtSource": "first_published",
  "descriptionRedacted": true,
  "requisitionId": "REQ-4821",
  "isNew": true,
  "scrapedAt": "2026-08-26T03:00:12Z"
}
```

```json
{
  "recordType": "company_summary",
  "provider": "greenhouse",
  "companySlug": "anthropic",
  "company": "Anthropic",
  "status": "ok",
  "jobsFound": 533,
  "jobsKept": 37,
  "newJobs": 3,
  "duplicatesDropped": 0,
  "trackedSince": "2026-08-24",
  "topDepartments": [{ "name": "Engineering", "count": 21 }],
  "scrapedAt": "2026-08-26T03:00:14Z"
}
```

```json
{
  "recordType": "error",
  "provider": "lever",
  "companySlug": "acme-that-does-not-exist",
  "status": "not_found",
  "error": "Lever returned 404 for both api.lever.co and api.eu.lever.co — the site name is probably wrong (Lever site names are case-sensitive)",
  "scrapedAt": "2026-08-26T03:00:15Z"
}
```

Two dataset views are provided: **Jobs** (spreadsheet-ready postings) and **Company summaries**. Export either as JSON, CSV, Excel or XML from the Storage tab.

**What `null` means: we could not determine it.** We never guess remote status, employment type, seniority or salary. A null field is the honest answer, and it is far more useful than an invented one when you are filtering thousands of rows.

## Filters, deduplication and normalization

- **`remote: null`** means neither the ATS, the location text nor the title said anything. Greenhouse, Personio and Rippling have no remote flag at all, so nulls are common there. `remoteOnly` keeps only positively-confirmed remote jobs, and `remoteSource` tells you which rule fired (`ats`, `location`, `title`, `description`).
- **Salary** is taken from the ATS's own structured fields first (`salarySource: "ats"`). Only when there is none do we run a conservative regex over the salary text (`salarySource: "parsed"`), with rejection gates for equity, bonuses, funding rounds, `401(k)`, date ranges and phone numbers. **Never an LLM.** For Greenhouse boards that publish several locale ranges, we pick the range matching the job's own country or currency rather than the first one in the list.
- **Employment type** is normalized to `full_time`, `part_time`, `contract`, `temporary`, `internship` or `other`, and `employmentTypeSource` says whether it came from the ATS (`ats`) or was inferred from the title (`title`). `strictEmploymentType` keeps only `ats`.
- **Dedupe by id** is always on. **Dedupe by content** additionally merges rows with the same title, company, raw location and requisition id — useful for boards that list a role once per office, but it can merge genuinely separate openings of the same role. The ids it dropped are listed in `dedupedFrom` on the surviving row, and counted in `duplicatesDropped` on the company summary. Leave it off unless you see duplicates.
- **Locations** are parsed into city, region, country and an always-upper-case `countryCode` from a bundled country/subdivision table. No geocoding, no coordinates, no external service. Multi-location postings keep every location in a sorted `locations[]` array.

## Privacy and contact redaction

- The output has **no contact, recruiter or candidate fields**. Ever. There is nothing to redact in the structured part of a row because none of it is personal data. `includeRawJson` is held to the same rule: contact-shaped keys and ad bodies are stripped from `raw`, and what is left is run through the same redaction as the description.
- **`includeDescription` ships on**, so a default run carries the ad body. That body is the **employer's own copyrighted advertisement text** — publishing an ad licenses nobody to redistribute it, and no ATS vendor holds a right it could pass on. You are the controller of what lands in your dataset; if you would rather not hold ad bodies at all, set `includeDescription: false` and you get the structured fields only.
- Employer-written ads — especially German and Dutch ones on Personio and Recruitee — routinely close with a named contact person. With `redactContacts` on (the default), the body is stripped before output of: **email addresses** (role mailboxes like `jobs@` included — the pattern cannot tell them apart, and you have `applyUrl` anyway), **phone numbers** in international, `00` and national-trunk formats, **LinkedIn / Xing / WhatsApp / Calendly / Telegram** personal profile links and `@handles`, and **a person's name where the ad labels it as a contact** — `Ansprechpartner:`, `Ihre Ansprechpartnerin:`, `Kontakt:`, `Contactpersoon:`, `Contact:`, `Hiring manager:`, `Recruiter:`, `Questions? Contact`. The label survives so you can see what was taken: `Ihre Ansprechpartnerin: [redacted]`. `descriptionRedacted: true` records that something was removed.
- **What redaction does not catch, stated plainly — we do not claim "no PII":** a name in *running prose* with no contact label in front of it. "You will report directly to Anna Schmidt, our VP of Engineering" survives, and so does a postal address, a Skype id and a name split across HTML tags in a way our patterns miss. That is deliberate: a name recogniser loose enough to catch free-text names shreds the ad body you are paying for, and every capitalised word in a German sentence is a candidate. Redaction is a *contactability* control, and it is applied because it is right, not because it makes the data anonymous. If you are named in an ad body and want it out, that is a 48-hour unconditional removal: [TAKEDOWN.md](https://github.com/moonie0201/ats-jobs/blob/main/TAKEDOWN.md) · [PRIVACY.md](https://github.com/moonie0201/ats-jobs/blob/main/PRIVACY.md).
- Nothing is stored outside your own Apify account, and nothing is sent to the developer. The Actor opens no outbound connection to us and embeds no credential of any kind.
- Your `onlyNewJobs` state lives in a key-value store in your account, under the name you choose.
- **Erasure, objection and takedown** are honoured unconditionally within 48 hours, for rightholders and for people named in an ad alike — [TAKEDOWN.md](https://github.com/moonie0201/ats-jobs/blob/main/TAKEDOWN.md), [PRIVACY.md](https://github.com/moonie0201/ats-jobs/blob/main/PRIVACY.md). Email `mooniegilog@gmail.com` (subject `takedown` or `privacy`) if you would rather not post details in public.

## Pricing

**$0.002 per job row — the only event that costs real money.** Everything else on the Pricing tab is platform floor:

| Event | Price | What it is |
|---|---|---|
| `job` | **$0.002** | One job row delivered to your dataset. This is the price. |
| `apify-actor-start` | $0.00005 | Apify's platform default, charged once per run. |
| `delta-run` | $0.00001 | Instrumentation, recorded once per run when `onlyNewJobs` is on. The platform will not accept a $0.00 event price, so it sits at the floor — one cent per thousand monitoring runs. It exists so we can see that delta mode is used at all, and it buys us nothing else. |

Free, always: company summary rows, error rows, jobs removed by your filters, companies we never reached because your `maxJobs` cap fired, and companies that failed.

| Run shape | Charged rows | Cost |
|---|---|---|
| One company, 50 jobs (an agent query) | 50 | **$0.10** |
| Ten companies, 1,000 jobs (a backfill) | 1,000 | **$2.00** |
| 2,000 companies, `onlyNewJobs`, 40 new jobs (daily monitoring) | 40 | **$0.08** |

That is $2 per 1,000 jobs. Set `maxJobs` to bound any run, or `ACTOR_MAX_TOTAL_CHARGE_USD` to bound the spend directly — when that limit is reached the Actor stops pushing, writes `budget_exhausted` summaries and still finishes successfully.

## Monitoring new job postings (delta mode)

Turn on `onlyNewJobs` and give each monitoring task its own `stateKey`. The first run stores the baseline and returns everything; later runs return only ids that were not there before, so a daily 2,000-company watch costs a few cents rather than thousands of rows. `stateRetentionDays` (default 90) forgets ids that stopped appearing, so the store cannot grow forever. The state is a key-value store **in your account**, named by you — we never see it.

## Hiring history

Turn on `onlyNewJobs` and the Actor keeps a baseline of seen job ids and per-company first-seen dates in a named key-value store **in your own account**; `trackedSince` on each company row is the date that company first appeared under your `stateKey`.

**What we run on our side, stated plainly:** a separate private Actor takes a daily snapshot of which jobs are open at the companies in our public directory, so that hiring-velocity and time-to-fill products can exist later. It stores job id, title, location, department, remote flag, url and dates — **never** descriptions, salaries, recruiter names or contact details. It is retained for 400 days and then deleted automatically ([PRIVACY.md](https://github.com/moonie0201/ats-jobs/blob/main/PRIVACY.md)). It reads nothing from your runs, your account or your inputs, and it produces nothing this Actor delivers. If you want your company out of it, [TAKEDOWN.md](https://github.com/moonie0201/ats-jobs/blob/main/TAKEDOWN.md) — 48 hours, no questions.

## Integrations: n8n, Make, Zapier, Clay, Google Sheets, Slack

- **n8n** — the *Apify* node, **Run an Actor** operation, then *Get dataset items*. Schedule it and feed new jobs into your own database.
- **Make** — the Apify *Run an Actor* module plus *Watch dataset items*.
- **Zapier** — Apify's *Run Actor* action; map `title`, `company`, `url` and `postedAt` into your CRM.
- **Clay** — call the Actor as an HTTP enrichment step, keyed on the company slug, to attach live openings to an account row.
- **Google Sheets** — export the **Jobs** dataset view as CSV, or push items straight into a sheet from n8n/Make.
- **Slack** — filter on `isNew` in delta mode and post the new rows to a channel.

## For AI agents and MCP

This Actor is agent-friendly by construction: limited permissions, pay-per-event with a single event, no Standby mode, typed error rows instead of stack traces, and a `minimal` output profile that returns only `title, company, locationRaw, city, countryCode, remote, url, postedAt` to keep token cost down.

```json
{
  "companies": ["ashby:openai"],
  "outputProfile": "minimal",
  "maxJobs": 25,
  "titleKeywords": ["research engineer"]
}
```

Set `maxJobs` on every call, and set `ACTOR_MAX_TOTAL_CHARGE_USD` on the run to cap spend. Errors come back as dataset rows with `recordType: "error"` and a machine-readable `status`, so a tool call never has to parse a traceback.

## Limitations you should know before you buy

- Greenhouse, Personio and Rippling have **no remote flag**, so `remote` is often `null` on those boards.
- Greenhouse's job `url` is frequently the company's own careers domain, not a `greenhouse.io` link — that is the customer's own configuration, and `applyUrl` duplicates it because Greenhouse exposes no separate apply link.
- Greenhouse never reports `team` and never reports employment type; where you see one it was inferred from the title, and `employmentTypeSource` says so.
- **Rippling is slow, on purpose.** Its API documentation states a limit of 100 requests per 10 minutes, so we cap `api.rippling.com` at 0.16 requests/second and the adapter needs one detail call per job. A 50-job Rippling board therefore takes minutes, not seconds. The other five providers are unaffected. Rippling also needs that detail call for employment type, posting date and salary, so a `minimal` profile run leaves them null, and it omits posting dates more often than the other five.
- Recruitee will require a per-company token from **2027-02-10**; we will add an optional token input before then.
- Personio and Recruitee descriptions are redacted more often than US boards, and now lose a labelled contact *name* as well as the address. That is those ads' convention — a named contact at the end — not a bug in the data. Redaction is label-anchored, so a name written into running prose is **not** removed; see [Privacy and contact redaction](#privacy-and-contact-redaction).
- Lever site names are **case-sensitive**: `palantir` works, `Palantir` returns 404.
- **SmartRecruiters is not supported**: its governing API policy prohibits large-scale extraction and AI-driven API use. **Workable is not supported**: its documented anonymous endpoint returns zero jobs for every account we tested, so shipping it would mean shipping a provider that silently returns nothing.
- **Workday is not in this Actor.** It uses an undocumented internal API and lives in a separate listing.
- There is no keyword search without a company list yet; see the switching section above.

## Related Actors

Per-ATS listings with the same engine and the same schema — Greenhouse jobs, Lever jobs and Ashby jobs — plus a Workday listing, and a jobs-history listing built on daily snapshots.

## FAQ

**Is this legal?** We call each vendor's own public, unauthenticated job-board API, we honour robots directives and documented rate limits, we do not scrape career pages, we use no proxies and we touch no login-walled site — so there is no access-control question here.

Copyright is a separate question and we will not pretend otherwise: **the description body stays the employer's copyrighted text.** Publishing an ad does not license anyone to redistribute it, and no ATS vendor holds a right it could pass on. The structured fields — title, location, salary, dates, department, apply url — are facts and carry no such issue. The body ships **on by default** because every one of the six APIs returns it in the response we already make and you are billed per row either way, so withholding it charged you the same for less; the control that matters is not a default, it is the removal route, and any rightholder who wants their text out of this product gets it out in 48 hours with no argument: [TAKEDOWN.md](https://github.com/moonie0201/ats-jobs/blob/main/TAKEDOWN.md). If you plan to republish description bodies at scale, that is your call to make with the employer, not ours to license to you.

**Do I get the job description?** Yes, by default. All six providers hand us the ad body in the same response the job row comes from, and you are billed per row either way, so it costs you nothing extra. Set `includeDescription: false` for a slimmer dataset, or `descriptionFormat: "html"`/`"both"` if you want the markup. The body remains the employer's copyrighted text and contact details are redacted by default — see [Privacy and contact redaction](#privacy-and-contact-redaction).

**Does the description contain personal data?** Sometimes, and we will not claim otherwise. `redactContacts` (on by default) removes emails, phone numbers, LinkedIn/Xing/WhatsApp/Calendly/Telegram links and handles, and names introduced by a contact label such as `Ansprechpartner:` or `Recruiter:`. It does **not** remove a name written into running prose. Anyone named in an ad body gets it removed in 48 hours, unconditionally: [TAKEDOWN.md](https://github.com/moonie0201/ats-jobs/blob/main/TAKEDOWN.md) · [PRIVACY.md](https://github.com/moonie0201/ats-jobs/blob/main/PRIVACY.md).

**Do I need an API key?** No. None of the six endpoints requires a credential, and the Actor stores none.

**How fresh is the data?** It is fetched live at run time. There is no index and no cache between you and the board.

**How do I get the company list?** Paste career-site URLs you already have, or use prefixed slugs. You can also open an issue on the repo to add a company to the public ATS directory that directory mode will use.

**Company not found?** Every entry needs a prefix (`lever:palantir`) or a career-site URL — a bare company name does not resolve until directory mode ships. If a prefixed slug still fails, check the slug casing on Lever and confirm the board is really hosted by that ATS.

**Why is `remote` null?** Because nothing said otherwise. See Filters, deduplication and normalization.

**Can I get salaries for every job?** No. Structured pay ranges depend entirely on what the employer published. Ashby and Greenhouse boards carry them often; Personio has none at all.

**How do I schedule it daily?** Use Apify Schedules, with `onlyNewJobs: true` and a dedicated `stateKey`.

**How do I export to CSV?** Storage → the **Jobs** view → Export → CSV. Or `GET /datasets/{id}/items?format=csv&view=jobs`.

## Support and issues

Open an issue on the Actor's Issues tab or in the public GitHub repository at
<https://github.com/moonie0201/ats-jobs>. Every issue gets a reply within 14 days, usually
within 48 hours. Bug reports that include the run id and the input are fixed fastest.

**Removal, takedown, copyright or privacy requests jump the queue and are answered within 48
hours:** [TAKEDOWN.md](https://github.com/moonie0201/ats-jobs/blob/main/TAKEDOWN.md) ·
[PRIVACY.md](https://github.com/moonie0201/ats-jobs/blob/main/PRIVACY.md).

## Disclaimer

This Actor is unofficial. It is not affiliated with, endorsed by, or sponsored by Greenhouse Software, Lever, Ashby, Recruitee, Rippling or Personio. It uses each vendor's public job-board API to retrieve publicly published job advertisements. All trademarks belong to their respective owners.

Job advertisement text remains the copyright of the employer that wrote it. The structured
fields this Actor produces are factual. Removal requests are honoured within 48 hours:
[TAKEDOWN.md](https://github.com/moonie0201/ats-jobs/blob/main/TAKEDOWN.md).
