# Job Posting Age & Lifecycle Enricher

> **Unofficial.** Not affiliated with Lever, Ashby, Rippling or any employer. Data comes
> from the unauthenticated public job-board APIs those platforms publish, plus a daily
> record of what those boards returned. Job facts are facts; the body of a posting is the
> employer's copyright and is never stored or returned.
> **Removal requests:** honoured in 48 h — see [TAKEDOWN.md](https://github.com/moonie0201/ats-jobs/blob/main/TAKEDOWN.md).

Give it a dataset of job rows. It tells you **how long each posting has been up**.

Every job-data product tells you what is open. Almost none tell you how long it has been
open, and the ones that do stop at the board's own posted date. This one adds that date
where it exists, our own first sighting where it does not, and — the part that matters —
says plainly when it cannot tell you.

## Use it as an integration

This Actor is built to be the *target* of another Actor. On any Actor that produces job
rows, open **Integrations → Add integration**, pick this one, and leave `datasetId` at its
prefilled `{{resource.defaultDatasetId}}`. Every run of that Actor then hands its dataset
here and you get the lifecycle columns back, in your own account.

It also runs standalone: pass a `datasetId` yourself.

**The upstream dataset has to be readable.** This Actor reads it over the public items
endpoint, which needs no token — set the source dataset's access to *Anyone with ID can
read* (Console → the dataset → Settings, or `generalAccess: ANYONE_WITH_ID_CAN_READ` over
the API). A private dataset is refused: an Actor runs under limited permissions and its
scoped token cannot open a dataset it did not create, even one in the same account.
Measured 2026-09-08 — the same dataset failed private and succeeded public, unchanged
otherwise.

## What comes back

```json
{
  "sourceUrl": "https://jobs.ashbyhq.com/ramp/34413f8d-…",
  "provider": "ashby", "company": "ramp",
  "coverage": "tracked",
  "postedAt": "2026-04-07", "postedAtSource": "publishedAt",
  "ageDays": 154, "ageBasis": "board",
  "firstSeen": "2026-09-04", "firstSeenCensored": true,
  "historyTrackedSince": "2026-09-04", "historyWindowDays": 4,
  "stillListed": true
}
```

Read that row carefully, because it is the honest shape of the answer. We had watched that
board for **four days**, and the posting is **154 days old** — because Ashby publishes its
own date and that beats our first sighting. `firstSeenCensored: true` says the posting was
already up when we started looking, so `firstSeen` is a floor, not a birthday.

## What it will not do

**`coverage` is the field to branch on.** Only `tracked` carries lifecycle facts:

| coverage | meaning |
|---|---|
| `tracked` | board is watched and the posting was found |
| `job_unknown` | board is watched, this id is not on it today |
| `board_untracked` | we have never watched this board |
| `unsupported_provider` | a provider whose URL cannot identify one posting |
| `unresolved_url` | not a job URL we recognise |

`stillListed` is `true` or `null` — **never `false`**. A posting missing from a board we
watch is not an observed removal: we may have started watching after it closed, or your row
may be stale. Saying `false` there would be an inference dressed as a fact.

**Three providers, and the reason is identity.** Lever, Ashby and Rippling put the board
API's own job key in the posting URL path, so the match is exact. Greenhouse redirects to
the employer's own domain with the id in a `gh_jid` query parameter, so its URL shape
differs per company; Recruitee URLs carry a mutable title slug and no id at all; Personio
URLs do carry a numeric id, but its boards are the one provider we hold under terms that
forbid republishing, so they are excluded here for that reason rather than a technical one.
All three come back as `unsupported_provider` rather than matched by guesswork, because a
wrong identity produces a confidently wrong history.

**History starts 2026-08-26**, and later for boards added since. `historyWindowDays` is how
long we have watched *that* board. A one-day window cannot support "this job never closes",
and the field is there so nobody reads it that way.

## Pricing

One `lifecycle` event per row that actually delivers an age. Rows that could not be
resolved, providers we cannot identify, boards we do not watch, ids we have never seen, and
duplicates of a posting already answered in the same run are **all returned and none are
charged**. Charging for "we do not know" is how a data product loses the trust it sells.

## Where the data comes from

A separate scheduled job reads the public board APIs of 14,243 companies across six ATS
platforms once a day and records what each board returned. The closure aggregates are
published as CC0: <https://github.com/moonie0201/hiring-closures>.
