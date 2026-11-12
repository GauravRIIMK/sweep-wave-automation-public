# Sweep wave automation

> **Public snapshot.** This copy shows the code, tests and GitHub Actions workflows. Runtime
> state (`state/`), one-off lead-specific scripts and personal assets are not included, and
> real people's names and emails in tests and code have been replaced with synthetic examples.
> The live system runs from a private repository.

Daily, unattended job-search follow-ups from Gmail Account A
(`rathore.gaurav1007@gmail.com`), running on GitHub Actions.

Every run classifies leads from the mailbox itself, researches each person
individually, writes a bespoke reply **inside their existing thread**, validates it against
the tone doctrine, and creates the draft. It does this for the current backlog and for any
lead first contacted in the future, forever, without being told about them.

## What one run does

| Step | What happens | Why it is in this order |
|---|---|---|
| 1. Identity gate | `users/me/profile` must equal the expected address | Deterministic. A search heuristic can be fooled by a send-as alias; the profile endpoint cannot |
| 2. Capacity gate | counts pending drafts, allows only `DAILY_CAP - pending` new ones | The mailbox dispatches a limited number per day. Creating more just builds a stale queue that later fires over replies |
| 3. Discovery | adds threads mailed since the last run to the candidate pool | Keeps the wave alive for people contacted after this repo was built |
| 4. Classify | fetches the **full thread** and decides | The Gmail thread is the only authority; a local ledger is a cache |
| 5. Research | LinkedIn-first, confidence gated | A wrong-person match is worse than no personalisation |
| 6. Craft | one bespoke mail per lead from thread + research + canonical facts | No templates. Each mail is written for that person |
| 7. Validate | 10 machine checks; one retry with the complaints; else hold | A draft here auto-sends, so a bad mail must never be created |
| 8. Persist | state committed back to the repo, report in the job summary | Full audit trail per run |

## Queue guard

Drafts are not sent the moment they are created: this mailbox dispatches in one daily
burst (observed 04:03 to 04:35 UTC). With a deep queue a draft can wait days, and a
reply arriving in that gap would otherwise be mailed over.

A guard therefore runs twice: at the start of every daily batch, and again at 03:45 UTC
(09:15 IST) immediately before the burst, as its own cheap job with no model calls.

It trashes a queued draft only when **a human replied in that thread AFTER the draft was
created**, and only for an address this pipeline tracks. That timing comparison is what
makes it safe to run unattended: a hand-written reply answers a message older than
itself and is never touched, while a stale follow-up is the opposite case. Rescued leads
move to the replied hold so they are never drafted again, and each rescue is reported
with the name and how long the gap was.

## Verdicts

- **ELIGIBLE** - quiet at least `MIN_QUIET_DAYS`, under the 6-touch cap, no reply, no bounce,
  no prior promise of silence, real first name known from the thread. Gets a crafted draft.
- **REPLY_DUE** - a human answered and the ball is in your court. **Never auto-drafted.**
  Listed in the run summary so you can answer personally.
- **SKIP** - with the exact reason recorded (blocked org, cap, bounce, too recent, and so on).

## Safety properties

- Never mails a banned or rejected org, a do-not-mail address, a personal mailbox of a
  corporate lead, a machine address, or a domain owned by the other mailbox.
- Never exceeds 6 total touches per human, counted from the thread itself.
- Never writes over a reply, and never re-opens a thread where silence was promised.
- Never invents a number: every figure in a mail must be canonical, or present in the
  research payload or the thread. Enforced in code, not by prompt.
- `concurrency` plus a same-day guard make double-batching impossible.
- `--dry-run` crafts and validates without creating anything.

## Setup

See [SETUP.md](SETUP.md). Three Gmail secrets and one Anthropic key; about five minutes.

## Configuration (repo variables, all optional)

| Variable | Current | Meaning |
|---|---|---|
| `DAILY_CAP` | 300 | Max drafts allowed to sit in the queue |
| `MIN_QUIET_DAYS` | 3 | Minimum silence before writing to someone again |
| `ABANDON_DAYS` | 30 | Drafts older than this stop counting toward the queue |
| `WEB_SEARCH_MAX_USES` | 6 | Web searches the researcher may run per lead |
| `CRAFT_MODEL` | claude-sonnet-5 | Model that writes the mails |
| `RESEARCH_MODEL` | claude-sonnet-5 | Model that does the web research |

Hard limits set in code, not variables: `TOUCH_CAP` = 6 lifetime mails per human
(`pipeline/registries.py`), mail length 70 to 260 words, one run per calendar day.

## Manual control

- Actions tab, "Daily outreach batch", Run workflow, with `dry_run` checked to preview.
- To pause: disable the workflow in the Actions tab.
- To stop mailing someone forever: add them to `BLOCK_ADDR` in `pipeline/registries.py`.
