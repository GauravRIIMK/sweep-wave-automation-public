# -*- coding: utf-8 -*-
"""Paced, gated dispatch of queued drafts.

WHY THIS EXISTS
---------------
Until 2026-09-10 this pipeline created drafts in the cloud and stopped. The SEND was
manual: the mailbox owner opened the Gmail Android app and fired the whole drafts folder
in one burst (measured 226 messages between 04:03 and 04:35 UTC on 09-09, about 7 per
minute). On 09-09/09-10, 84 of those messages wedged in the app's Outbox at
Queued / Uploading / Sending and never left.

Root cause, established from the mailbox rather than from forums: Gmail rate-limits a
burst like that with **403 userRateLimitExceeded** (not 429, not an SMTP bounce), and the
Android client does not surface the refusal. It retries silently and parks the message in
Outbox forever. That is why force-stop, network switch and cache clear all failed -- every
one of them is a device-side remedy for a server-side refusal.

`gmail_api.py` already defends the draft-CREATION path against exactly this (pacing plus
403-aware backoff, lines 51-76). This module extends the same discipline to the send path
so the phone never has to dispatch anything again.

THE FIVE INVARIANTS. Each one exists because its absence caused real damage.

  1. NEVER RETRY A SEND. /drafts/send has no idempotency key. A retry after a lost
     response delivers twice, to a real person. Compare the comment in gmail_api.api():
     the same reasoning already forced retries=1 on POST /drafts after a 502-then-retry
     produced a second identical draft that then auto-sent.

  2. A RATE-LIMIT HALTS THE WHOLE RUN. Not "skip and try the next one". The allowance is
     per-account, so if one send is refused the next will be too, and hammering it is what
     deepens the restriction. On any quota signal we stop and record a stand-down.

  3. RE-CHECK FOR A REPLY IMMEDIATELY BEFORE EACH SEND. queue_guard runs at 03:45 UTC;
     a dispatch at 04:30 is 45 minutes later, and a deep queue can take hours to drain.
     A reply landing inside that window would otherwise be mailed over -- the single most
     embarrassing failure this system can produce. The check is the same narrow rule
     queue_guard uses: a HUMAN inbound strictly NEWER than the draft.

  4. ONLY DRAFTS THIS PIPELINE AUTHORED. Checked by DRAFT ID, not by recipient.
     This invariant was written first and implemented wrong: the code checked only
     whether the RECIPIENT appeared in pool.json + sent_log.json, which is an allowlist
     of 1,799 addresses the pipeline may ever mail, not a record of what it wrote. An
     adversarial audit reproduced the consequence against the real state files: a
     half-finished personal reply the owner parked in the drafts folder, addressed to any
     tracked lead, was picked up and delivered by the cloud job.
     That is a live hazard here because (a) queue_guard deliberately PRESERVES
     hand-written drafts (tests/test_queue_guard.py asserts it), so dispatch would send
     exactly what the guard protects, and (b) run_daily.py:182-186 records that this
     mailbox accumulates long-dead drafts nothing ever sends. Two other tools also write
     into this same folder.
     run_daily records the draft id it created in every sent_log row (196/196 carry one),
     so authorship is knowable. If that state is ever lost the allowlist shrinks and
     legitimate drafts are skipped: it fails closed, which is the right direction.

  5. AMBIGUOUS FAILURES STOP THE RUN AND ARE REPORTED, NOT SWALLOWED. If a send raises
     after the request left (timeout, 5xx), we genuinely do not know whether it was
     delivered. Guessing either way is wrong, so we halt and surface it for a human.

QUOTA DAY: counted on the UTC date, matching the rest of this repo. Google's own send
quota resets at midnight US-Pacific (12:30 PM IST), so the two windows are offset. The
daily cap is therefore set well below the real ceiling; the offset cannot matter if we
never approach the limit.
"""
import datetime
import os
import time

from . import classify
from . import registries as R
from . import tone
from .gmail_api import RESUME_NAME

# Error fragments that mean "the account is refusing sends right now".
QUOTA_SIGNALS = (
    "userratelimitexceeded",
    "ratelimitexceeded",
    "quotaexceeded",
    "too many concurrent requests",
    "limit for sending",
    "service invoked too many",
    "4.7.28",
)


# Reasons a draft can NEVER become sendable. A draft blocked for one of these is not
# waiting for anything: it is dead mail that will sit in the folder forever, where the
# mailbox owner still fires it by hand (a draft here IS a send), and where it consumes
# one of the day's DAILY_CAP slots that a live lead could have used. Trashing them is the
# only thing that actually retires them.
# Deliberately NOT here: "human replied" (queue_guard owns that), "tone doctrine" (the
# copy could be rewritten), and anything "unreadable" (transient). And a draft that is
# not ours never reaches this code at all, so a hand-written note can never be purged.
PURGE_REASONS = ("on holds.json", "blocked:", "silence promised", "touch cap")


def is_permanent_block(reason):
    r = (reason or "").lower()
    return any(p in r for p in PURGE_REASONS)


def is_quota_error(exc):
    m = str(exc).lower()
    return any(s in m for s in QUOTA_SIGNALS)


def send_block_reason(g, lead, thread_id, draft_ms, held, draft_id=None,
                      draft_msg_id=None, extra_touches=None):
    """Every reason NOT to send, evaluated at SEND time. None means go.

    WHY THIS RE-CHECKS WHAT THE DRAFTER ALREADY CHECKED
    ---------------------------------------------------
    The drafter applies its gates when the draft is CREATED. Dispatch happens later, and
    with DAILY_CAP 300 against a queue that drains ~200/day, "later" can be days. In that
    window the world changes, and it demonstrably does:

      * On 2026-09-10 a promise-of-silence filter bug was fixed and the corpus re-scanned.
        307 leads turned out to have been promised silence. 88 went into holds["closed"],
        82 were still in the active pool, and THREE already had drafts queued to send:
        aanya_chauhan@apple.com, ishanieaswaran@morganstanley.com, poojanaidu@pg.com.
        Those three were trashed by hand. All three are still in `tracked`.
      * Deloitte closed on 2026-08-27 while 16 drafts to it sat in the folder.

    A dispatcher that trusts creation-time gating would have mailed every one of them.
    Holds are therefore re-read from disk on every run and applied here, at the last
    moment before delivery. This is the cheapest guard in the system and the only one
    that sees the world as it is at send time rather than as it was at draft time.

    Fails CLOSED throughout: an unreadable thread is not evidence of safety.
    """
    # 1. Holds. Re-read every run precisely because a lead can be held AFTER its draft
    #    was created. Covers replied / bounced / capped / closed, same union run_daily
    #    builds at run_daily.py:245.
    if classify.canon(lead) in held:
        return "on holds.json"

    # 2. Org, domain and address bans (BLOCK_ORG_TOKENS / BLOCK_DOMAIN_SUFFIX / BLOCK_ADDR).
    #    Reuses classify.blocked_reason so the ban list can never drift between the
    #    drafter and the dispatcher.
    br = classify.blocked_reason(lead)
    if br:
        return "blocked: %s" % br

    # 3. Thread-derived checks. ONE fetch serves both, because a second call per draft
    #    would double this job's Gmail call volume for no new information.
    try:
        msgs = g.thread_full(thread_id)
    except Exception as e:
        return "thread unreadable (%s)" % e
    if not msgs:
        return "thread unreadable or empty"

    # 3a. A human replied after the draft was queued. Same narrow rule as
    #     queue_guard.sweep(): a hand-written reply answers a message OLDER than itself
    #     and is never caught; a stale follow-up has the inbound NEWER than the draft.
    for m in msgs:
        if (m["sender"] not in R.ME
                and not classify.is_non_human(m["sender"])
                and m["date_ms"] > draft_ms):
            return "human replied after this draft was queued"

    # 3b. We promised to stop writing in this thread. Mirrors classify.py:179-183 exactly:
    #     scan m["text"] (the DEQUOTED body) on messages from ME addressed to THIS lead.
    #     Using all_text instead would match a promise sitting in quoted history and
    #     block threads where no promise was ever actually made.
    #     Only messages STRICTLY OLDER than the draft count, which also excludes the
    #     draft itself if the API returns it inside the thread.
    lead_c = classify.canon(lead)
    for m in msgs:
        # Exclude the draft itself BY ID. Excluding by timestamp (>= draft_ms) also
        # discarded every genuine outbound sent AFTER the draft was queued, so a
        # close-loop mail sent by the owner on day 2 through other tooling -- "this is my
        # last note, I will stop here" -- was invisible, and the queued follow-up shipped
        # on day 3 straight over the top of the promise.
        if draft_msg_id and m.get("id") == draft_msg_id:
            continue
        if m["sender"] in R.ME and classify.canon(m.get("to") or "") == lead_c:
            if classify.PROMISE.search(m.get("text") or ""):
                return "silence promised in this thread"

    # 3c. TOUCH CAP, re-counted at send time. The drafter checks it at creation, but the
    #     owner mails these same people out of band (all 196 sent_log rows come from a
    #     tool outside this pipeline), so a lead at 5 touches on day 1 can be at 6 by the
    #     time the draft dispatches. Counts our outbound to THIS lead in this thread,
    #     excluding the draft, plus any touches recorded elsewhere.
    in_thread = sum(1 for m in msgs
                    if m.get("id") != draft_msg_id
                    and m["sender"] in R.ME
                    and classify.canon(m.get("to") or "") == lead_c)
    total = in_thread + int((extra_touches or {}).get(lead_c, 0))
    if total >= R.TOUCH_CAP:
        return "touch cap reached (%d/%d)" % (total, R.TOUCH_CAP)

    # 4. TONE AND VOICE DOCTRINE, re-checked on the finished body.
    #    validate.py gates tone when a mail is CRAFTED. Drafts already sitting in the
    #    folder were written before that gate existed and were never tone-checked at all,
    #    and the doctrine keeps moving: the release tail was REQUIRED until 2026-08-26
    #    and is BANNED now, so the older a draft is the likelier it violates. Checking
    #    here means a doctrine change applies to everything still unsent, not only to
    #    mail written after the change.
    #    Fails CLOSED: an unreadable body is not evidence of a clean one.
    if draft_id:
        try:
            body = g.draft_body(draft_id)
        except Exception as e:
            return "draft body unreadable (%s)" % e
        if not body:
            return "draft body unreadable or empty"
        try:
            blocking, _ = tone.tone_violations(body, touch="followup")
        except Exception as e:
            return "tone gate failed to run (%s)" % e
        if blocking:
            return "tone doctrine: " + "; ".join(blocking)[:200]

    return None


MIN_SEND_BYTES = int(os.environ.get("MIN_SEND_BYTES", "50000"))


def day_bounds_ms(day):
    """(start_ms, end_ms) for a UTC date string 'YYYY-MM-DD'. Raises on a bad date.

    UTC, to match every other date in this repo: the run reports, the daily-cap counter
    and the once-a-day guard are all keyed on the UTC date, so a drain scoped to "today"
    has to mean the same day they do.
    """
    d = datetime.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
    return int(d.timestamp() * 1000), int((d + datetime.timedelta(days=1)).timestamp() * 1000)


def inventory(g, tracked, held=None, authored=None, extra_touches=None,
              created_on=None, log=print):
    """Every draft, and the single reason it is not sendable. Sends nothing.

    Deliberately runs the SAME gates in the SAME order as dispatch(), so the answer is
    not an approximation of what a drain would do: it is what a drain would do. A separate
    re-implementation would drift, and a report that disagrees with the sender is worse
    than no report, because it is believed.
    """
    window = day_bounds_ms(created_on) if created_on else None
    rows, summary = [], {"in_folder": 0, "in_window": 0, "sendable": 0, "reasons": {}}

    def note(reason):
        summary["reasons"][reason] = summary["reasons"].get(reason, 0) + 1

    drafts = g.list_drafts()
    summary["in_folder"] = len(drafts)
    for d in drafts:
        try:
            dd = g.draft_detail(d["draft_id"])
        except Exception as e:
            note(f"draft_detail failed ({str(e)[:60]})")
            continue
        if not dd or not dd.get("to") or not dd.get("threadId"):
            note("unreadable, or missing recipient or thread")
            continue
        created = int(dd.get("internalDate") or 0)
        if window and not (window[0] <= created < window[1]):
            continue
        summary["in_window"] += 1
        lead = dd["to"].lower()
        size = int(dd.get("sizeEstimate") or 0)
        names = g.attachments(dd.get("msg_id"))
        row = {"draft_id": d["draft_id"], "to": lead, "size": size,
               "created_ms": created, "thread_id": dd.get("threadId"),
               "attachments": names,
               "has_resume": any(n.lower() == RESUME_NAME.lower() for n in names),
               "authored": d["draft_id"] in (authored or set())}

        if not row["authored"]:
            row["reason"] = "not authored by this pipeline"
        elif classify.canon(lead) not in tracked:
            row["reason"] = "recipient not tracked"
        elif not row["has_resume"]:
            row["reason"] = f"no resume attached (size {size})"
        else:
            row["reason"] = send_block_reason(
                g, lead, dd["threadId"], created, held, draft_id=d["draft_id"],
                draft_msg_id=dd.get("msg_id"), extra_touches=extra_touches)
        if row["reason"] is None:
            summary["sendable"] += 1
        else:
            note(row["reason"].split("(")[0].strip())
        rows.append(row)
        if len(rows) % 25 == 0:
            log(f"  inventoried {len(rows)} ({summary['sendable']} sendable so far)")

    by_org = {}
    for r in rows:
        if r["reason"] is None:
            dom = r["to"].split("@")[-1]
            by_org[dom] = by_org.get(dom, 0) + 1
    summary["sendable_by_domain"] = dict(sorted(by_org.items(), key=lambda kv: -kv[1]))
    log(f"inventory: {summary['in_folder']} in folder, {summary['in_window']} in window, "
        f"{summary['sendable']} sendable")
    return {"created_on": created_on, "summary": summary, "drafts": rows}


def dispatch(g, tracked, held=None, authored=None, sent_today=0, daily_cap=120,
             max_per_run=10, pace_seconds=25.0, dry_run=True, log=print,
             sleep=time.sleep, extra_touches=None, created_on=None, report=None):
    """Send up to max_per_run queued drafts, paced.

    created_on ('YYYY-MM-DD') restricts the run to drafts CREATED on that UTC day and is
    the only way to drain one day's output without touching the backlog behind it. The
    folder holds drafts going back weeks; "send what was written today" is a different
    request from "send the queue", and conflating them mails people the owner never
    asked to mail on this pass.

    Returns (report, errors). report["halted"] is set to a reason string when the run
    stopped early; the caller MUST persist report["sent"] before exiting either way, or
    the daily counter drifts and the cap stops binding.
    """
    # The caller may pass its own dict so that a run killed mid-drain still knows what it
    # delivered. Returning the report is no use when the return never happens: GitHub
    # cancels a job with SIGINT, which unwinds straight past the return statement, and the
    # caller was then left holding its initial {"sent": 0}. Every message already sent
    # went unrecorded, the daily counter did not move, and the cap stopped binding.
    if report is None:
        report = {}
    report.update({"attempted": 0, "sent": 0, "skipped": 0, "rescued": 0, "purged": 0,
                   "halted": None, "quota": False, "ambiguous": [], "detail": []})
    errors = []
    # None is NOT the same as empty here. An empty set means "no holds", which is a real
    # state; None means the caller forgot to pass them, and silently treating that as
    # "nothing is held" is exactly how a promised-silence lead gets mailed. Fail loud.
    if held is None:
        raise ValueError("dispatch() requires `held`; pass an explicit set (empty is fine)")
    # Same reasoning: defaulting a missing authorship record to "allow everything" is how
    # a hand-written draft gets delivered. An empty set is a valid state (send nothing).
    if authored is None:
        raise ValueError("dispatch() requires `authored`; pass an explicit set of draft ids")

    remaining_today = max(0, daily_cap - sent_today)
    if remaining_today <= 0:
        report["halted"] = f"daily cap reached ({sent_today}/{daily_cap})"
        log(f"dispatch: {report['halted']}")
        return report, errors

    budget = min(max_per_run, remaining_today)

    window = None
    if created_on:
        window = day_bounds_ms(created_on)

    try:
        drafts = g.list_drafts()
    except Exception as e:
        return report, [f"list_drafts failed: {e}"]

    log(f"dispatch: {len(drafts)} drafts in folder, budget {budget} this run, "
        f"{sent_today}/{daily_cap} used today, dry_run={dry_run}"
        + (f", scoped to drafts created {created_on}" if created_on else ""))

    for d in drafts:
        if report["sent"] >= budget:
            break
        try:
            dd = g.draft_detail(d["draft_id"])
        except Exception as e:
            errors.append(f"draft_detail {d['draft_id']}: {e}")
            continue
        if not dd or not dd.get("to") or not dd.get("threadId"):
            continue

        # DAY SCOPE, when the caller asked for one. Checked before authorship so the
        # skip counters describe the day being drained rather than the whole folder.
        # Reads dd, not the list row: draft_detail is the one place internalDate is
        # guaranteed, which is the same lesson the size guard learned the hard way.
        if window:
            created = int(dd.get("internalDate") or 0)
            if not (window[0] <= created < window[1]):
                continue

        lead = dd["to"].lower()

        # INVARIANT 4, part 1: AUTHORSHIP. Only drafts this pipeline created may be sent.
        # Keyed on the draft id recorded at creation, never on the recipient: the
        # recipient set is an allowlist of everyone we MAY mail, which says nothing about
        # who wrote this particular draft. `authored` empty means "no authorship record",
        # and we then refuse everything rather than fall back to the recipient check.
        if d["draft_id"] not in authored:
            report["skipped"] += 1
            report["detail"].append({"email": lead, "action": "SKIP",
                                     "reason": "not authored by this pipeline"})
            continue

        # INVARIANT 4, part 2: the recipient must still be one we track. Redundant given
        # the authorship check, kept as defence in depth because it is free.
        if classify.canon(lead) not in tracked:
            continue

        # SIXTH INVARIANT, added 2026-09-17: never deliver a draft with no resume.
        # 44 went out bare before this existed, then 7 more after a first attempt read the
        # size off the wrong dict. This is the THIRD cut, and the first one that actually
        # asks the question: does this message carry a part called Gaurav_Rathore_Resume.pdf?
        #
        # Size was never a test of that. A reply quoting a long inbound hit 141,762 bytes
        # with nothing attached, and the 50,000 floor was an eighth of what the resume
        # weighs on the wire, so a bare mail with a chatty quote sailed straight over it.
        # Both failure directions were live at once. Fails CLOSED: an unreadable part list
        # comes back empty, which refuses.
        size = int(dd.get("sizeEstimate") or 0)
        try:
            carries_resume = g.has_resume(dd.get("msg_id"))
        except Exception as e:
            if is_quota_error(e):
                # INVARIANT 2 applies to the READ path too. The allowance is per account,
                # so a refusal here means the next draft will be refused as well, and
                # continuing would relabel the rest of the queue "no resume attached".
                report["halted"] = f"rate limited reading {lead}: {e}"
                report["quota"] = True
                log(f"  HALT: account is refusing reads ({e})")
                errors.append(report["halted"])
                return report, errors
            carries_resume = False
        if not carries_resume:
            report["skipped"] += 1
            report["detail"].append({"email": lead, "action": "SKIP",
                                     "reason": f"no resume attached (size {size})"})
            continue

        report["attempted"] += 1

        # INVARIANT 3: re-check EVERY gate right before sending, not once at the top of
        # the run and not only at draft-creation time. See send_block_reason.
        reason = send_block_reason(g, lead, dd["threadId"], dd.get("internalDate") or 0,
                                   held, draft_id=d["draft_id"],
                                   draft_msg_id=dd.get("msg_id"),
                                   extra_touches=extra_touches)
        if reason:
            report["skipped"] += 1
            if "replied" in reason:
                report["rescued"] += 1
            action = "SKIP"
            if not dry_run and is_permanent_block(reason):
                try:
                    g.trash_message(dd["msg_id"])
                    report["purged"] += 1
                    action = "PURGED"
                except Exception as e:
                    errors.append(f"purge {lead}: {e}")
            report["detail"].append({"email": lead, "action": action, "reason": reason})
            log(f"  {action} {lead}: {reason}")
            continue

        if dry_run:
            report["sent"] += 1          # consume budget so a dry run paces realistically
            report["detail"].append({"email": lead, "action": "DRY_RUN"})
            log(f"  DRY RUN {lead}: would send")
            continue

        try:
            msg = g.send_draft(d["draft_id"])
        except Exception as e:
            # INVARIANT 2: a quota signal stops everything.
            if is_quota_error(e):
                report["halted"] = f"rate limited on {lead}: {e}"
                # Only THIS arms the 26-hour stand-down. Reaching the daily cap and an
                # ambiguous 502 both also set report["halted"], and the caller used to
                # treat any halt as a quota event, so a run that simply finished its
                # allowance stood the account down until the following afternoon. That is
                # very likely what happened on 2026-09-17.
                report["quota"] = True
                log(f"  HALT: account is refusing sends ({e})")
                errors.append(report["halted"])
                return report, errors
            # INVARIANT 5: we cannot tell whether this one went out. Stop and surface it.
            report["halted"] = f"ambiguous send failure on {lead}: {e}"
            report["ambiguous"].append({"email": lead, "draft_id": d["draft_id"],
                                        "error": str(e)[:200]})
            log(f"  HALT: ambiguous failure for {lead} ({e}). "
                f"Check Sent before re-running, this may or may not have been delivered.")
            errors.append(report["halted"])
            return report, errors

        report["sent"] += 1
        report["detail"].append({"email": lead, "action": "SENT",
                                 "msg_id": (msg or {}).get("id")})
        log(f"  SENT {lead}")

        if report["sent"] < budget:
            sleep(pace_seconds)

    log(f"dispatch: sent={report['sent']} skipped={report['skipped']} "
        f"rescued={report['rescued']} halted={report['halted']}")
    return report, errors
