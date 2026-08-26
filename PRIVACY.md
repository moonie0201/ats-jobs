# Privacy notice

This is the notice for **the developer of the `ats-jobs-scraper` Actor and the maintainer of
the `ats-directory` dataset**. If you ran the Actor yourself, you are the controller of the
dataset it wrote into your own Apify account, and Apify's own
[privacy policy](https://apify.com/privacy-policy) governs the platform.

Last updated: 2026-08-26.

## What we hold, and what we do not

| Data | Where it lives | Do we hold it? |
|---|---|---|
| Job rows a run produced | The buyer's own Apify dataset | **No.** The Actor opens no outbound connection to us and sends us nothing. |
| The buyer's `onlyNewJobs` state | A key-value store in the buyer's account, named by the buyer | **No.** |
| Company directory rows — `(provider, slug, region, name, domain, status)` | Public, CC0 | Yes. |
| Daily job-metadata snapshots — id, title, location, department, remote flag, url, dates | A private Apify key-value store on our account | Yes. |
| Descriptions, salaries, recruiter names, contact details, applicant data | — | **Never stored by us, anywhere.** The snapshot store holds none of it by construction. |

We run no analytics, set no cookies, and operate no website that collects anything.

## Where personal data can appear anyway

Two places, and we name them rather than claiming there are none:

1. **Inside an ad body.** Employers — especially German- and Dutch-language SMEs — close a
   posting with a named contact person. `includeDescription` defaults to **on**, so a default
   run carries that text through to the buyer. `redactContacts` also defaults to **on** and
   removes email addresses, phone numbers, LinkedIn/Xing/WhatsApp/Calendly/Telegram profile
   links and handles, and a person's name **where the ad labels it as a contact**
   (`Ansprechpartner:`, `Kontakt:`, `Contactpersoon:`, `Contact:`, `Hiring manager:`,
   `Recruiter:`, `Questions? Contact`) — the label is kept so the buyer can see what was
   taken. It does **not** remove a name written into running prose ("you will report to Anna
   Schmidt"): there is no pattern there to match, and a recogniser loose enough to catch it
   destroys the advertisement. We therefore do not claim the output is free of personal data.
   `descriptionRedacted` on each row records whether anything was removed, and anyone named
   in an ad body can have it removed unconditionally within 48 hours (see below).
2. **A company name in the directory.** A sole trader trading under their own name is a
   natural person. A handful of directory rows are eponymous for that reason.

## Your rights, and how to use them

We honour **erasure (Art. 17), objection (Art. 21) and rectification (Art. 16)
unconditionally** — we do not run a balancing test against you, ask what your reasons are,
or require you to establish that you are the person concerned to a standard we invent. Say
the word and it goes.

Objection to processing for direct marketing (Art. 21(2)) is absolute; the Actor is sold in
part to sales teams, and an objection on that ground is honoured on receipt.

**How:** open an issue at <https://github.com/moonie0201/ats-jobs/issues> containing only
the words `private request` — nothing else, no personal details. We reply there with a
private channel within one business day and handle the substance off the public record.
Requests are actioned within **48 hours** of us having enough information to act.

There is no dedicated privacy mailbox yet; the GitHub route above is the real, monitored
channel and we would rather publish a channel that works than an address that does not.

## Retention

| Store | Kept for |
|---|---|
| `ats-history` daily event and count files | **400 days.** Older keys are deleted by an automatic sweep on every run (`core.history.HistoryStore.prune`). |
| `ats-history` current-state buckets | Until the company is removed or the board goes dark. |
| Directory rows | Until removed on request, or until the board stops answering. |
| Anything about a buyer, a run, or a query | Not collected, so nothing to retain. |

## Legal basis and territorial position

For the directory and the snapshot store we rely on **Art. 6(1)(f)** legitimate interests:
publishing a factual index of which employers use which public job-board API, and measuring
open-role counts over time. Neither store holds descriptions or contact details, so the
data those interests are balanced against is business information, not personal data, except
in the sole-trader tail named above. The unconditional erasure and objection route above is
the mitigating measure that balance rests on.

The operator is established in the **Republic of Korea** and is not established in the EU.
No EU or UK representative has been designated under Art. 27, on the assessment that the
service is offered to buyers rather than to the data subjects, and that no behaviour of
people in the Union is monitored. We publish that reasoning here rather than leaving it
implicit — if you disagree, the route above reaches us and the answer will not be "prove it".

## Changes

This file is versioned in a public repository. Its history is the changelog.
