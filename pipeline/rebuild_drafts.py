# -*- coding: utf-8 -*-
"""Rebuild body-only drafts so they carry the resume.

    python -m pipeline.rebuild_drafts [--dry-run] [--limit N]

WHY THIS EXISTS
---------------
Until 2026-09-17 `create_reply_draft` built a bare text/plain message, so every draft this
pipeline ever wrote had NO attachment. The owner sends from the Gmail Android app, where he
attaches the CV by hand; when that 300KB upload cannot finish the app parks the message in
the Outbox at "Uploading" and the server keeps the attachment-less copy. That is why 97
messages sat unsent while only 9 drafts in the mailbox carried a file: the bytes never
reached Google.

Sending those drafts as they stand delivers a job pitch with no CV behind it, which already
happened to 51 people. So they are REBUILT: same thread, same words, now multipart with
assets/Gaurav_Rathore_Resume.pdf, and the original is trashed only after its replacement
exists and has been confirmed.

EVERY GUARD BELOW EXISTS BECAUSE A REVIEW FOUND ITS ABSENCE WOULD CAUSE REAL DAMAGE:

  1. AUTHORSHIP. Only drafts this pipeline wrote, by draft id from sent_log. The folder also
     holds hand-written drafts that queue_guard deliberately preserves; rebuilding one would
     re-create somebody's personal mail with a job CV stapled to it and trash the original.
     An empty authorship set aborts the run rather than meaning "everything".
  2. THE LEDGER MOVES WITH THE DRAFT. sent_log's draft_id is rewritten old -> new as each
     rebuild succeeds. Without that, every rebuilt draft reads as "not authored" and the
     gated dispatcher refuses the entire queue, leaving the manual Android burst as the only
     route out, which is the thing that wedged 84 messages in the first place.
  3. TRASH IS VERIFIED. api() returns None on 404, so trashing a stale id "succeeds"
     silently. The old draft is re-fetched afterwards and a survivor is a hard error, with a
     consecutive-failure breaker so one systematic failure cannot duplicate the folder.
  4. THE ANCHOR IS NEVER THE DRAFT ITSELF. thread_full returns the draft inside its own
     thread, so msgs[-1] is usually the message being replaced. Anchoring there produces
     In-Reply-To pointing at something about to be trashed, or an empty subject shipped as
     literally "Re: ".
  5. UNKNOWN SIZE REFUSES. A draft whose size cannot be read might already carry a tailored
     attachment; rebuilding would replace it with the generic CV.
  6. NO SILENT RECIPIENT LOSS. The reply builder takes one To and no Cc, so a draft with
     extra recipients is skipped, not quietly narrowed.

Nothing here delivers mail. Sending stays with run_dispatch, which is paced and gated.
"""
import argparse
import datetime
import email.utils
import json
import os
import re
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import classify, registries as R
from pipeline.gmail_api import Gmail, EXPECTED_ACCOUNT, RESUME_PATH

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state")
MIN_SEND_BYTES = int(os.environ.get("MIN_SEND_BYTES", "50000"))
TRASH_FAIL_STREAK = int(os.environ.get("TRASH_FAIL_STREAK", "3"))

# Claims the factbank refutes. These entered the corpus through registries.CANDIDATE_FACTS,
# which has since been corrected, so they appear only in bodies crafted before 2026-09-17.
# A bullet carrying one is DROPPED (the mail reads fine with two bullets instead of three);
# the same claim outside a bullet quarantines the whole draft, because rewriting a sentence
# mechanically is how you invent a new falsehood while removing an old one.
BAD_CLAIMS = [
    (r"\d+\s*Mn\+?\s*(daily\s*)?impressions", "unconfirmed impressions figure"),
    (r"38\s+product\s+launches", "38 launches were DARK STORES at Blinkit, not upGrad products"),
    (r"Rs\.?\s*15\s*Cr", "Rs 15 Cr is the known 10x error for Rs 1.5 Cr"),
]


def log(msg):
    print(f"[{datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')}] {msg}",
          flush=True)


def jload(name, default):
    p = os.path.join(STATE, name)
    if not os.path.exists(p):
        return default
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return default


def jsave(name, obj):
    p = os.path.join(STATE, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def clean_body(body):
    """Drop whole bullets carrying a refuted claim. Returns (body, dropped, quarantine)."""
    lines, dropped, quarantine = body.splitlines(), [], []
    keep = []
    for ln in lines:
        hit = next((why for pat, why in BAD_CLAIMS if re.search(pat, ln, re.I)), None)
        if not hit:
            keep.append(ln)
            continue
        if ln.lstrip().startswith(("-", "*", "•")):
            dropped.append(hit)
            continue
        quarantine.append(hit)
        keep.append(ln)
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip()
    return out, dropped, quarantine


def anchor_for(g, thread_id, draft_msg_id, lead, fallback_subject):
    """The newest message that is NOT the draft, preferring our own outbound to this lead.

    thread_full returns the draft inside its own thread, so the naive msgs[-1] is usually
    the message about to be trashed. dispatch.py excludes it by id for the same reason, and
    classify anchors on the last outbound addressed to the lead; both rules are applied.
    """
    msgs = g.thread_full(thread_id)
    if not msgs:
        return None
    others = [m for m in msgs if m.get("id") != draft_msg_id and m.get("message_id_hdr")]
    if not others:
        return None
    mine = [m for m in others
            if m.get("sender") in R.ME and classify.canon(m.get("to", "")) == classify.canon(lead)]
    last = (mine or others)[-1]
    subj = last.get("subject") or fallback_subject
    if not subj.strip():
        return None
    return {"subject": subj, "message_id_hdr": last["message_id_hdr"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--allow-ids", default="", dest="allow_ids",
                    help="comma-separated draft ids to treat as authored although "
                         "sent_log never recorded them. For drafts written through "
                         "another tool against this same mailbox: they are real, they "
                         "are the owner's, and they carry no attachment because only "
                         "this module knows how to add one. Naming an id is a deliberate "
                         "human act; nothing infers it.")
    ap.add_argument("--payloads", default="",
                    help="JSON file of [{draft_id, body, subject}] written elsewhere. When "
                         "given, ONLY those drafts are processed and the supplied body "
                         "replaces the old one. Used to swap in re-crafted copy without "
                         "spending the pipeline's own API budget.")
    args = ap.parse_args()

    missing = [k for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")
               if not os.environ.get(k)]
    if missing:
        log(f"ABORT: missing secrets {missing}")
        return 4
    if not os.path.exists(RESUME_PATH):
        log(f"ABORT: resume not found at {RESUME_PATH}")
        return 4

    sent_log = jload("sent_log.json", [])
    by_draft = {r["draft_id"]: r for r in sent_log if r.get("draft_id")}
    if not by_draft:
        log("ABORT: sent_log.json carries no draft ids, so nothing can be proven authored.")
        return 5
    allowed = {x.strip() for x in (args.allow_ids or "").split(",") if x.strip()}
    if allowed:
        # These widen the eligible set; they do NOT replace it the way run_dispatch's
        # --allow-ids does. The difference is the blast radius: dispatch DELIVERS, so
        # naming ids there has to mean "only these". Rebuild only rewrites a draft in
        # place, and a rebuild that skipped the pipeline's own body-only drafts because
        # a human named five extra ones would quietly leave unsendable mail in the queue.
        log(f"allow-ids: {len(allowed)} draft ids accepted as authored for this run")
    holds = jload("holds.json", {})
    held = {classify.canon(a) for k in ("replied", "bounced", "capped", "closed")
            for a in (holds.get(k) or [])}

    g = Gmail()
    acct = g.profile_email()
    if acct != EXPECTED_ACCOUNT:
        log(f"ABORT: connected mailbox is {acct!r}, expected {EXPECTED_ACCOUNT!r}")
        return 2
    log(f"mailbox verified: {acct}; authored drafts on record: {len(by_draft)}; "
        f"resume {os.path.getsize(RESUME_PATH)} bytes")

    payloads = {}
    if args.payloads:
        raw = json.load(open(args.payloads, encoding="utf-8"))
        payloads = {p["draft_id"]: p for p in raw if p.get("draft_id") and p.get("body")}
        if not payloads:
            log(f"ABORT: {args.payloads} carried no usable payloads")
            return 5
        log(f"payload mode: {len(payloads)} re-crafted bodies supplied")

    drafts = g.list_drafts()
    log(f"{len(drafts)} drafts in folder")

    report = {"date": datetime.date.today().isoformat(), "dry_run": args.dry_run,
              "rebuilt": [], "quarantined": [], "skipped": {}, "errors": []}

    def skip(reason):
        report["skipped"][reason] = report["skipped"].get(reason, 0) + 1

    done, trash_fails = 0, 0
    for d in drafts:
        if args.limit and done >= args.limit:
            break
        if payloads and d["draft_id"] not in payloads:
            continue
        if d["draft_id"] not in by_draft and d["draft_id"] not in allowed:
            skip("not authored by this pipeline")
            continue
        try:
            dd = g.draft_detail(d["draft_id"])
        except Exception as e:
            report["errors"].append(f"draft_detail {d['draft_id']}: {e}")
            continue
        if not dd or not dd.get("to") or not dd.get("threadId") or not dd.get("msg_id"):
            skip("draft unreadable, or missing recipient, thread or message id")
            continue
        if "TRASH" in (dd.get("labels") or []):
            skip("already trashed by an earlier pass")
            continue
        size = int(dd.get("sizeEstimate") or 0)
        if size <= 0:
            skip("size unknown, refusing to touch it")
            continue
        # Same correction as dispatch.py on 2026-09-18: size is not evidence about an
        # attachment. Here the proxy failed the OTHER way round from the send guard. A
        # bare reply quoting a long inbound clears 50,000 easily, so this skipped it as
        # "already carries an attachment" and left a draft that the sender would then
        # refuse for having none. The draft sits in the folder forever, fixed by nothing.
        if g.has_resume(dd.get("msg_id")):
            skip("already carries the resume")
            continue
        if dd.get("cc_raw") or len(email.utils.getaddresses([dd.get("to_raw", "")])) > 1:
            skip("has Cc or multiple recipients, which the reply builder cannot carry")
            continue
        lead = dd["to"]
        if classify.canon(lead) in held:
            skip("lead is on holds.json")
            continue

        supplied = payloads.get(d["draft_id"])
        body = supplied["body"] if supplied else g.draft_body(d["draft_id"])
        if not body or len(body.strip()) < 40:
            skip("body empty or unreadable")
            continue
        # The claim check runs on SUPPLIED copy too. New copy is written against the
        # corrected fact sheet, but this is the last gate before a real person reads it.
        body, dropped, quarantine = clean_body(body)
        if quarantine:
            report["quarantined"].append({"to": lead, "draft_id": d["draft_id"],
                                          "reasons": sorted(set(quarantine))})
            continue

        anchor = anchor_for(g, dd["threadId"], dd["msg_id"], lead, dd.get("subject", ""))
        if anchor and supplied and supplied.get("subject"):
            anchor["subject"] = supplied["subject"]
        if not anchor:
            skip("no threadable anchor with a Message-ID and a subject")
            continue

        if args.dry_run:
            report["rebuilt"].append({"to": lead, "old": d["draft_id"], "new": None,
                                      "dropped_bullets": sorted(set(dropped)), "dry_run": True})
            done += 1
            continue

        try:
            nd = g.create_reply_draft(lead, dd["threadId"], anchor, body)
        except Exception as e:
            report["errors"].append(f"create {lead}: {e}")
            continue
        new_id = (nd or {}).get("id")
        if not new_id:
            report["errors"].append(f"create {lead}: no draft id returned")
            continue

        # Trash the original ONLY now, and CONFIRM it. A 404 from the API returns None
        # rather than raising, so "no exception" proves nothing on its own.
        gone = False
        try:
            g.delete_draft(d["draft_id"])
            after = g.draft_detail(d["draft_id"]) or {}
            # Either proof is enough: the draft is gone, or it is labelled TRASH. The first
            # cut asked only whether the record still existed, which a trashed draft answers
            # yes to, and the second asked for a label Gmail had not applied yet.
            gone = (not after) or ("TRASH" in (after.get("labels") or []))
        except Exception as e:
            report["errors"].append(f"delete old {lead}: {e}")
        if not gone:
            trash_fails += 1
            report["errors"].append(
                f"DUPLICATE RISK {lead}: new draft {new_id} exists but old {d['draft_id']} survives")
            if trash_fails >= TRASH_FAIL_STREAK:
                log(f"HALT: {trash_fails} trash failures in a row. Every further rebuild would "
                    f"duplicate a real mail. Stopping with {done} rebuilt.")
                break
        else:
            trash_fails = 0

        # The ledger follows the draft, or the dispatcher will refuse what we just built.
        # An allow-listed id has no row yet, so one is created rather than mutated: this
        # is the record that makes the rebuilt draft sendable at all, and without it the
        # rebuild would attach the resume and the dispatcher would then refuse to send it.
        if d["draft_id"] in by_draft:
            by_draft[d["draft_id"]]["draft_id"] = new_id
        else:
            # "thread_id", not "threadId". Every consumer in the repo reads the former,
            # so the camelCase spelling wrote a row the recycle pass and the day ledger
            # both look straight through. It reads as a typo and behaves as data loss.
            row = {"email": lead, "draft_id": new_id, "thread_id": dd["threadId"],
                   "source": "rebuild-allow-ids",
                   "date": datetime.date.today().isoformat()}
            sent_log.append(row)
            by_draft[new_id] = row
        report["rebuilt"].append({"to": lead, "old": d["draft_id"], "new": new_id,
                                  "dropped_bullets": sorted(set(dropped))})
        done += 1
        if done % 10 == 0:
            jsave("sent_log.json", sent_log)
            log(f"rebuilt {done}")

    if not args.dry_run:
        jsave("sent_log.json", sent_log)
    report["summary"] = {"rebuilt": len(report["rebuilt"]),
                         "quarantined": len(report["quarantined"]),
                         "skipped": sum(report["skipped"].values()),
                         "errors": len(report["errors"]),
                         "bullets_dropped": sum(len(r.get("dropped_bullets") or [])
                                                for r in report["rebuilt"])}
    jsave(f"rebuild-{datetime.date.today().isoformat()}.json", report)
    log(json.dumps(report["summary"]))
    log(json.dumps(report["skipped"]))
    for q in report["quarantined"][:15]:
        log(f"  QUARANTINED {q['to']}: {'; '.join(q['reasons'])}")
    for e in report["errors"][:10]:
        log(f"  ERROR {e}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
