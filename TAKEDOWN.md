# Takedown and removal

We honour every removal request within **48 hours**, without argument and without asking
you to justify it. You do not need to be a lawyer, send a formal notice, or prove ownership
to a standard we set.

## How to reach us

**Email `mooniegilog@gmail.com`** — this is the fastest route and the right one for anything involving
personal data or that you do not want on a public page. Put `takedown` in the subject.

Or **open an issue**, whichever is closer to what you want removed:

- Code, test fixtures, Actor output, or anything about the scraper —
  <https://github.com/moonie0201/ats-jobs/issues>
- A company row in the public directory — <https://github.com/moonie0201/ats-directory/issues>
- If you bought a run and the problem is with the delivered data, the Actor's **Issues** tab
  at <https://apify.com/acotr_moonie/ats-jobs-scraper/issues> reaches the same person.

Issues are public. **If your request involves personal data or anything you do not want
published, open an issue containing only the words `private request` and nothing else** — we
will reply there with a private channel within one business day, and you never have to post
the details in the open.

Title the issue `TAKEDOWN` or `REMOVAL` so it is not queued behind bug reports.

## What we will do

| You are | You want | What happens |
|---|---|---|
| An employer whose ad text we reproduced | The text gone | We add the board to the blocklist. The company's rows stop being fetched, stop being published in the directory, and are purged from the history store. |
| A company that does not want to be listed at all | The row gone | The `(provider, slug)` pair goes into `blocklist.txt`. Every subsequent build excludes it from `companies.jsonl`, `companies.jsonl.gz` and `core/data/companies.seed.jsonl.gz`. |
| An ATS vendor | Us off your API | We disable the adapter. There is no negotiation step; tell us and it stops. |
| A person named in a job ad | Your data gone | See [`PRIVACY.md`](PRIVACY.md). Erasure and objection are honoured unconditionally. |

## The limit we cannot fix, stated plainly

The public directory is published under **CC0 1.0**, which is an irrevocable dedication. We
can remove a row from this repository and we do. We **cannot** un-publish copies that other
people already downloaded, forked, or mirrored to jsDelivr before the removal. That is a
property of the licence, not a policy of ours, and you should know it before you decide
whether removal is enough for you.

The same is true of buyers' datasets. A run delivers rows into the buyer's own Apify account
and we hold no copy and no connection to it, so we can stop future runs but cannot reach
into a dataset someone already has.

## Blocklist

`blocklist.txt` in the [directory repository](https://github.com/moonie0201/ats-directory)
is the mechanism, not a promise about one. One `provider:slug` per line, `#` comments
allowed. `scripts/build_directory.py` applies it before writing any output file, and its
`--selfcheck` asserts that it does — so a blocked company cannot come back through a rebuild
or through the seed file baked into the Actor image.

## No affiliation

This project is unofficial. It is not affiliated with, endorsed by, or sponsored by
Greenhouse Software, Lever, Ashby, Recruitee, Rippling, Personio, Apify, or any employer
whose job openings it indexes. All trademarks belong to their owners.
