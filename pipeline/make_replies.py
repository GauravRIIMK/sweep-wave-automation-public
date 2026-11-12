# -*- coding: utf-8 -*-
"""Create in-thread REPLY drafts that carry the resume.

    python -m pipeline.make_replies --payloads state/reply_payloads.json [--dry-run]

WHY THIS EXISTS
---------------
Replies to real humans were being written straight into Gmail through the MCP connector,
which cannot attach a file. In this mailbox that is not a cosmetic problem: the owner's
Android client dispatches drafts by itself within minutes, so a draft here is effectively
a send. On 2026-09-18 twelve such replies left bare, and the owner's standing rule is that
nothing goes out without Gaurav_Rathore_Resume.pdf behind it.

gmail_api.create_reply_draft already builds a multipart message with the PDF and REFUSES
to build one without it. This module is the front door to it for hand-authored copy: the
bodies come from a file, verbatim, and nothing here writes prose.

WHAT IT REFUSES TO DO, and why each guard exists
------------------------------------------------
  * Wrong mailbox. Same identity gate as every other entry point. A send-as alias can fool
    a search heuristic, so profile identity is checked directly.
  * No resume on disk. create_reply_draft raises; this checks first so the run dies before
    it has half-created a batch.
  * A payload whose thread cannot be read, or whose anchor message is not in that thread.
    Anchoring to a message id from a different thread produces a draft that looks like a
    reply and is not one.
  * ALREADY ANSWERED. If an outbound from us exists AFTER the anchor message, the thread
    has moved on and this payload is stale. This is the guard that matters most: these
    payloads are written from a sweep that may be hours old, and the cheapest way to
    embarrass someone is to answer a question they already had answered.
  * A DUPLICATE draft already sitting in the folder for that thread.
  * A hard org, domain or address ban (classify.blocked_reason).

Deliberately NOT refused: holds.json. Its buckets are replied / bounced / capped / closed,
and every one of those describes COLD OUTREACH. A human who wrote a question is owed an
answer whatever bucket they sit in, and refusing here once buried a live salary question
for five weeks. The owner decides who is answered; this module only checks it is really
a reply and that the CV is attached.

Exit codes: 0 ok, 2 wrong mailbox, 4 missing secrets or resume, 5 nothing usable.
"""
import argparse
import datetime
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import classify
from pipeline.gmail_api import Gmail, EXPECTED_ACCOUNT, RESUME_PATH, RESUME_NAME
from pipeline import registries as R

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state")


def log(m):
    print(f"[{datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')}] {m}",
          flush=True)


def jsave(name, obj):
    p = os.path.join(STATE, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--payloads", required=True,
                    help="JSON list of {to, body, and EITHER thread_id (+ optional "
                         "reply_to_msg_id) for an in-thread reply OR subject for a cold "
                         "mail that opens a new thread}. "
                         "Bodies are used VERBATIM; this module never edits copy.")
    ap.add_argument("--dry-run", action="store_true",
                    help="run every guard and report, create nothing")
    args = ap.parse_args()

    missing = [k for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")
               if not os.environ.get(k)]
    if missing:
        log(f"ABORT: missing secrets {missing}")
        return 4
    if not os.path.exists(RESUME_PATH):
        log(f"ABORT: resume not found at {RESUME_PATH}")
        return 4

    rows = json.load(open(args.payloads, encoding="utf-8-sig"))
    rows = [r for r in rows if r.get("to") and r.get("body")
            and (r.get("thread_id") or r.get("subject"))]
    if not rows:
        log(f"ABORT: {args.payloads} carried no usable payloads")
        return 5

    g = Gmail()
    acct = g.profile_email()
    if acct != EXPECTED_ACCOUNT:
        log(f"ABORT: connected mailbox is {acct!r}, expected {EXPECTED_ACCOUNT!r}")
        return 2
    log(f"mailbox verified: {acct}; {len(rows)} payloads; "
        f"resume {os.path.getsize(RESUME_PATH)} bytes as {RESUME_NAME}")

    existing = {}
    for d in g.list_drafts():
        existing.setdefault(d.get("threadId"), []).append(d["draft_id"])

    report = {"date": datetime.date.today().isoformat(), "dry_run": args.dry_run,
              "created": [], "skipped": [], "errors": []}

    def skip(row, why):
        log(f"  SKIP {row.get('to')}: {why}")
        report["skipped"].append({"to": row.get("to"), "reason": why})

    # Addresses that already have ANY thread, so a "cold" payload cannot silently open a
    # second conversation with someone we are already talking to. That is the one-thread
    # -per-lead rule, and it is checked against the mailbox rather than against the payload.
    for row in rows:
        lead = row["to"].strip()
        tid = (row.get("thread_id") or "").strip()
        anchor_id = (row.get("reply_to_msg_id") or "").strip()

        if not tid:
            # COLD PATH: no thread named. Verify there genuinely is none before opening
            # one, because a payload written from a stale sweep would otherwise start a
            # second thread with someone mid-conversation.
            try:
                prior = g.search_messages(f"to:{lead}", cap=3)
            except Exception as e:
                report["errors"].append(f"{lead}: prior-thread check failed: {e}")
                skip(row, f"prior-thread check failed ({e})")
                continue
            if prior:
                skip(row, "a thread already exists; a cold payload must not open a second one")
                continue
            if not (row.get("subject") or "").strip():
                skip(row, "a new thread needs a subject")
                continue
            br = classify.blocked_reason(lead)
            if br:
                skip(row, f"blocked: {br}")
                continue
            if args.dry_run:
                log(f"  WOULD OPEN new thread -> {lead}: {row['subject'][:60]!r}")
                report["created"].append({"to": lead, "thread_id": None, "draft_id": None,
                                          "new_thread": True, "dry_run": True})
                continue
            try:
                nd = g.create_new_draft(lead, row["subject"], row["body"])
            except Exception as e:
                report["errors"].append(f"{lead}: create failed: {e}")
                log(f"  ERROR {lead}: {str(e)[:80]}")
                continue
            did = (nd or {}).get("id")
            log(f"  CREATED (new thread) {lead} -> {did}")
            report["created"].append({"to": lead, "draft_id": did, "new_thread": True})
            continue

        # The ban list is deferred until after the anchor is resolved, below: it is a rule
        # about who we may COLD-MAIL, and it must never decide who we may answer.
        if existing.get(tid):
            skip(row, f"a draft already exists in this thread ({existing[tid][0]})")
            continue

        try:
            msgs = g.thread_full(tid)
        except Exception as e:
            report["errors"].append(f"{lead}: thread unreadable: {e}")
            skip(row, f"thread unreadable ({e})")
            continue
        if not msgs:
            skip(row, "thread unreadable or empty")
            continue

        anchor_msg = next((m for m in msgs if m.get("id") == anchor_id), None)
        if anchor_id and not anchor_msg:
            skip(row, f"anchor message {anchor_id} is not in thread {tid}")
            continue
        if not anchor_msg:
            # No anchor named: fall back to the newest inbound from a human.
            inbound = [m for m in msgs
                       if m.get("sender") not in R.ME and not classify.is_non_human(m.get("sender", ""))]
            if not inbound:
                skip(row, "no human inbound to reply to")
                continue
            anchor_msg = inbound[-1]

        # ALREADY ANSWERED. Anything of ours newer than the anchor means the thread moved
        # on after the message this payload was written against.
        newer_ours = [m for m in msgs
                      if m.get("sender") in R.ME
                      and int(m.get("date_ms") or 0) > int(anchor_msg.get("date_ms") or 0)]
        if newer_ours:
            skip(row, f"already answered: {len(newer_ours)} outbound after the anchor")
            continue

        if not anchor_msg.get("message_id_hdr"):
            skip(row, "anchor carries no Message-ID header, cannot thread a reply")
            continue

        # BANS APPLY TO COLD MAIL, NOT TO ANSWERS. classify.blocked_reason refuses
        # personal mailboxes and ATS/machine domains, which is right for outreach and
        # wrong here. On the first live pass it skipped a Thapar graduate who had written
        # asking for help, and contact2@amazon.jobs, which is a live Customer Lifecycle
        # Manager interview where a recruiter signing herself Mary Catharin had asked to
        # be told if no confirmation arrived. Both were people, both were owed an answer,
        # and a domain pattern said otherwise.
        #
        # The rule that is actually true: you may always answer the address that wrote to
        # you. So the ban is overridden only when the anchor message was SENT BY the lead
        # we are replying to. When it was not, the ban stands, because then this is not a
        # reply at all.
        br = classify.blocked_reason(lead)
        if br:
            anchor_from = classify.canon(anchor_msg.get("sender") or "")
            if anchor_from != classify.canon(lead):
                skip(row, f"blocked: {br} (and the anchor was not sent by them)")
                continue
            log(f"  NOTE {lead}: {br}, overridden because they wrote this message "
                f"themselves and an answer is owed")

        anchor = {"subject": anchor_msg.get("subject") or row.get("subject", ""),
                  "message_id_hdr": anchor_msg["message_id_hdr"]}
        if not (anchor["subject"] or "").strip():
            skip(row, "no subject to reply under")
            continue

        if args.dry_run:
            log(f"  WOULD CREATE -> {lead} in {tid} under {anchor['subject'][:60]!r}")
            report["created"].append({"to": lead, "thread_id": tid, "draft_id": None,
                                      "dry_run": True})
            continue

        try:
            d = g.create_reply_draft(lead, tid, anchor, row["body"])
        except Exception as e:
            report["errors"].append(f"{lead}: create failed: {e}")
            log(f"  ERROR {lead}: {e}")
            continue
        did = (d or {}).get("id")
        if not did:
            report["errors"].append(f"{lead}: no draft id returned")
            continue
        log(f"  CREATED {lead} -> {did}")
        report["created"].append({"to": lead, "thread_id": tid, "draft_id": did})

    report["summary"] = {"created": len(report["created"]),
                         "skipped": len(report["skipped"]),
                         "errors": len(report["errors"])}
    jsave(f"replies-{datetime.date.today().isoformat()}.json", report)
    log(json.dumps(report["summary"]))
    for s in report["skipped"]:
        log(f"  skipped {s['to']}: {s['reason']}")
    for e in report["errors"][:10]:
        log(f"  ERROR {e}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
