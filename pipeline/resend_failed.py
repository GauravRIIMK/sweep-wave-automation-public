# -*- coding: utf-8 -*-
"""Find drafts that were released but never actually sent, and rebuild them.

    python -m pipeline.resend_failed --domains swiggy.in,flipkart.com --days 2 [--dry-run]

THE FAILURE THIS REPAIRS
------------------------
Gmail caps a consumer account at roughly 500 sends a day. When the owner's phone drains
a large drafts folder it eventually crosses that line, and what happens next is quietly
destructive: the client removes the draft as though it had been sent, and the server
answers with a mailer-daemon notice saying "You have reached a limit for sending mail.
Your message was not sent."

So the draft is GONE and the mail never left. It looks, from the drafts folder, exactly
like success. On 2026-09-24 this happened repeatedly, and the daemon notices carry the
template copy from that morning's wave, which is how it was caught.

Note that this is NOT the Outbox wedge. There the message sits visibly stuck at
"Sending". Here it vanishes, which is worse, because nothing shows you the loss.

HOW A FAILURE IS IDENTIFIED
---------------------------
For each person a run reports it drafted in the window, ask Gmail whether any message to
them exists in Sent on or after that day. That is the only honest test:

  - a run report says a draft was CREATED, never that it was delivered
  - the drafts folder cannot answer, because the draft is gone either way
  - the daemon notice names the failure but not always the recipient

No sent message on the day the draft was made means the send failed, and the person is
owed a fresh draft.

WHAT IT REBUILDS
----------------
The same in-thread reply, carrying the resume, through create_reply_draft, which refuses
to build a message without the PDF. Copy comes from template_craft, so no Anthropic call
is made and the rebuild works with a dead credit balance. It is deterministic per
address, so the person receives the same mail that was written for them the first time.

SUPPRESSION
-----------
This module creates drafts in a mailbox whose drafts leave within hours, so it carries its
own suppression gate and reads every source fresh on every run. It does not rely on the
tone validator reached through template_craft to do that for it: that guard is one keyword
argument deep in another module, and one edit there would reopen this path silently.

Five sources, each able to veto on its own, all applied before any craft or draft create:

  1. state/holds.json          bounced / capped / closed            SUPPRESSING_BUCKETS
  2. do-not-mail registry      address scope, and org:<domain>      state/suppression.json
  3. promise-of-silence        my own promises, from the ledgers    state/suppression.json
  4. banned and rejected orgs  classify.blocked_reason(), plus the
                               domains registries.py still misses   state/suppression.json
  5. the thread in hand        classify.PROMISE over my own
                               outbound, once the thread is read    thread_promise()

Source 5 is the one that cannot go stale. Sources 2 to 4 come from state/suppression.json
because their real sources are NOT in the repo -- the memory registry lives outside it and
the promise ledgers are untracked -- so a GitHub Actions checkout cannot see any of them.
tools/build_suppression.py compiles that file and must be re-run, and committed, whenever
the registry or a promise ledger changes. A gate is only a gate where its source exists.

Exit codes: 0 ok, 2 wrong mailbox, 4 missing secrets or resume or unreadable holds,
5 nothing to do.
"""
import argparse
import datetime
import glob
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import classify, template_craft
from pipeline import registries as R
from pipeline.gmail_api import Gmail, EXPECTED_ACCOUNT, RESUME_PATH

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state")


def log(m):
    print(f"[{datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')}] {m}",
          flush=True)


# WHICH BUCKETS BAR A REBUILD
#
#   bounced  the address is dead. A rebuild cannot arrive and costs sender reputation.
#   capped   the human is at the touch cap. A rebuild is touch seven.
#   closed   the conversation or the org is over, by decision.
#
# "replied" is NOT a bar here. A human who answered is owed an answer, and withholding the
# repair would strand a mail that was written for them and then lost. It is reported as a
# note instead, because what this module rebuilds is a TEMPLATE follow-up: somebody
# mid-conversation usually wants a written reply rather than the next template, and the
# note puts that in front of a human without the module quietly dropping the lead.
#
# "review" is NOT a bar either, and that is a deliberate departure from run_dispatch.py,
# which does union it. The two modules are looking at different things. run_dispatch walks
# a folder of drafts whose copy was written days ago and never re-validated, so a review
# entry is the freshest signal it has about them. This module writes new copy for every
# lead through template_craft -> validate, which re-tests the very conditions the review
# bucket records: ungrounded claims, missing signature fields, length, tone rule ids. A
# defect that still holds therefore still blocks the rebuild, on its own merits, at this
# module's craft step. gate_review_holds.py states the project's position in its own words
# -- "'review' is the bucket we are working FROM, so it cannot also be the thing that
# blocks" -- and of the 201 addresses in review today, 126 sit in no suppressing bucket at
# all while 81 of those were drafted successfully at least once. Treating sediment from an
# earlier run as a permanent bar would strand their repairs over a copy defect that a
# fresh craft may not even reproduce.
SUPPRESSING_BUCKETS = ("bounced", "capped", "closed")
NOTED_BUCKETS = ("replied", "review")


def _bucket_addrs(holds, bucket):
    """Canonical addresses in one holds.json bucket, whatever shape the bucket has.

    Lists carry addresses; "review" is a dict keyed by address. Both shapes are live in
    that file, so both are read, and anything else is a corrupt file rather than an empty
    bucket. Parsed as JSON by the caller and compared as WHOLE canonical addresses: a
    substring ban on "lever.co" once blocked every "unilever.com" address in this corpus,
    so nothing here ever matches a fragment or a bare domain.
    """
    v = holds.get(bucket)
    if v is None:
        return set()
    if isinstance(v, dict):
        items = list(v.keys())
    elif isinstance(v, list):
        items = v
    else:
        raise RuntimeError(f"holds.json bucket {bucket!r} is a {type(v).__name__}, "
                           "expected a list or an object")
    out = set()
    for e in items:
        if not isinstance(e, str):
            continue
        c = classify.canon(e)
        if "@" in c and not c.startswith("@"):
            out.add(c)
    return out


def load_holds(path=None):
    """Read state/holds.json and return ({canon: bucket} barred, {canon: bucket} noted).

    Read fresh on every run, never cached, because a lead can be held AFTER the draft this
    module is repairing was created -- which on this path is the normal case rather than
    the edge one: the mail it is repairing failed to send days ago.

    Raises RuntimeError instead of degrading to an empty set. A gate that cannot read its
    own source must stop the run, not wave it through: holds.json carries over 1,200
    addresses on this account, so "no holds" is a failed read, never a state.
    """
    p = path or os.path.join(STATE, "holds.json")
    if not os.path.exists(p):
        raise RuntimeError(f"holds.json not found at {p}")
    try:
        holds = json.load(open(p, encoding="utf-8-sig"))
    except Exception as e:
        raise RuntimeError(f"holds.json is unreadable ({e})")
    if not isinstance(holds, dict):
        raise RuntimeError(f"holds.json is a {type(holds).__name__}, expected an object")

    barred = {}
    for b in SUPPRESSING_BUCKETS:
        for c in _bucket_addrs(holds, b):
            barred.setdefault(c, b)
    if not barred:
        raise RuntimeError("holds set is empty across "
                           + "/".join(SUPPRESSING_BUCKETS)
                           + ", which is not a state this account should be in. Refusing "
                             "to rebuild unguarded.")
    noted = {}
    for b in NOTED_BUCKETS:
        for c in _bucket_addrs(holds, b):
            noted.setdefault(c, b)
    return barred, noted


def load_suppression(path=None):
    """Read state/suppression.json -> ({canon addr: reason}, {domain: reason}).

    Built by tools/build_suppression.py from the do-not-mail registry and the promise
    ledgers, none of which a checkout can see. Raises rather than degrading, for the same
    reason load_holds does: in CI this file IS the registry, and an unreadable gate must
    stop the run instead of waving it through.
    """
    p = path or os.path.join(STATE, "suppression.json")
    if not os.path.exists(p):
        raise RuntimeError(
            f"suppression.json not found at {p}. Run tools/build_suppression.py and "
            "commit it: the do-not-mail registry and the promise ledgers are not in "
            "this repository, so without it this run has no promise-of-silence gate.")
    try:
        d = json.load(open(p, encoding="utf-8-sig"))
    except Exception as e:
        raise RuntimeError(f"suppression.json is unreadable ({e})")
    if not isinstance(d, dict):
        raise RuntimeError(f"suppression.json is a {type(d).__name__}, expected an object")

    addrs, domains = {}, {}
    # Promise first, so that when a lead is caught by several sources the REPORTED reason
    # is the most serious one. A promise breach is the worst failure this system can make:
    # the recipient can see the quoted promise on the same screen as the new mail.
    for key, prefix in (("promised_silence", "PROMISE OF SILENCE: "),
                        ("do_not_mail", "")):
        v = d.get(key)
        if v is None:
            continue
        if not isinstance(v, dict):
            raise RuntimeError(f"suppression.json {key!r} is a {type(v).__name__}, "
                               "expected an object")
        for a, why in v.items():
            c = classify.canon(a)
            if "@" in c and not c.startswith("@"):
                addrs.setdefault(c, prefix + str(why)[:160])
    for key in ("do_not_mail_orgs", "registry_gap_domains"):
        v = d.get(key)
        if v is None:
            continue
        if not isinstance(v, dict):
            raise RuntimeError(f"suppression.json {key!r} is a {type(v).__name__}, "
                               "expected an object")
        for dom, why in v.items():
            dom = str(dom).strip().lower().lstrip("@")
            if dom and "." in dom:
                domains.setdefault(dom, str(why)[:160])

    if not addrs:
        raise RuntimeError("suppression.json carries no suppressed addresses, which is "
                           "not a state this account should be in. Refusing to rebuild "
                           "unguarded.")
    return addrs, domains


def domain_bar(addr, domain_bars):
    """Reason this address's domain is barred, or None.

    EXACT or SUBDOMAIN SUFFIX, never a substring. A substring ban on "lever.co" once
    blocked every address at unilever.com, removing 21 legitimate leads at a target
    company, and "hire.com" would take "shire.com" the same way.
    """
    dom = (addr or "").split("@")[-1].strip().lower()
    if not dom:
        return None
    for d in sorted(domain_bars):
        if dom == d or dom.endswith("." + d):
            return domain_bars[d]
    return None


def suppressed_reason(lead, addr_bars, domain_bars):
    """Why this lead must not be rebuilt, or None. The whole decision, in one place.

    Both arguments are required and neither may be None: dispatch() takes the same line
    with its `held` set, because "nothing is suppressed" is exactly how a promised-silence
    lead gets mailed.
    """
    if addr_bars is None or domain_bars is None:
        raise ValueError("suppressed_reason() requires explicit bar sets")
    c = classify.canon(lead)
    if c in addr_bars:
        return addr_bars[c]
    d = domain_bar(c, domain_bars)
    if d:
        return d
    # The shared registry: banned and rejected orgs, ATS and machine domains, personal
    # mailboxes, Account B's domains. Reused rather than copied so it cannot drift.
    #
    # classify.classify() deliberately runs this AFTER its reply check, because applying
    # an org ban to somebody who WROTE TO US once buried a Vedantu hiring contact for five
    # weeks. That reasoning does not reach this path: nothing here answers a human. This
    # module only rebuilds a TEMPLATE follow-up, so barring it buries no inbound -- the
    # reply surface belongs to run_daily and queue_guard -- and sending a template to
    # someone at a closed org is the error, not the silence.
    br = classify.blocked_reason(lead)
    if br:
        return f"blocked by registries.py: {br}"
    return None


def thread_promise(msgs, lead):
    """Evidence of a promise of silence in MY OWN outbound in this thread, or None.

    Mirrors pipeline/classify.py:classify exactly -- classify.PROMISE over m["text"], for
    my outbound addressed to this lead -- so the two readings of "did I promise to stop"
    cannot drift apart. state/suppression.json is a SNAPSHOT of past audits; this reads the
    thread actually in hand, so it also catches a promise made after the last one ran.
    """
    lead_c = classify.canon(lead)
    for m in (msgs or []):
        if m.get("sender") not in R.ME:
            continue
        if classify.canon(m.get("to") or "") != lead_c:
            continue
        hit = classify.PROMISE.search(m.get("text") or "")
        if hit:
            return "PROMISE OF SILENCE in this thread: " + hit.group(0)[:90]
    return None


def merge_bars(holds_barred, suppression_addrs):
    """One {canon addr: reason} map from the holds buckets and suppression.json.

    Lives here, not inline in main(), so that the CALLER's own logic is under test. The
    engine being right proves nothing if main() forgets to hand it a source: on
    2026-10-22 eleven correct tone rules shipped with the headline fix uncovered because
    it lived in the caller, one file away, and every test supplied the argument itself.

    Reason precedence is worst-first, because the reported reason is what a human reads:
    a promise of silence outranks a do-not-mail row, which outranks a holds bucket. The
    suppression map is already ordered that way internally, so it goes in first and the
    holds buckets only fill addresses it has not already claimed.
    """
    if holds_barred is None or suppression_addrs is None:
        raise ValueError("merge_bars() requires both sources; pass explicit maps")
    out = dict(suppression_addrs)
    for c, bucket in holds_barred.items():
        out.setdefault(c, f"on holds.json ({bucket})")
    return out


def gate_scope(scope, addr_bars, domain_bars):
    """Split {email: drafted day} into (rebuildable, barred_out).

    Runs ahead of every Gmail read, every craft and every draft create. Matched on
    classify.canon so a dot-variant or a +tag cannot slip past as a different person.
    """
    sendable, blocked = {}, []
    for e, d in sorted(scope.items()):
        why = suppressed_reason(e, addr_bars, domain_bars)
        if why:
            blocked.append({"to": e, "drafted": d, "reason": why})
        else:
            sendable[e] = d
    return sendable, blocked


def write_report(report):
    p = os.path.join(STATE, f"resend-{report['date']}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    return p


def drafted_in_window(days):
    """{email: earliest date a run says it drafted them} across the window's reports."""
    out = {}
    today = datetime.date.today()
    wanted = {(today - datetime.timedelta(days=d)).isoformat() for d in range(days + 1)}
    for f in sorted(glob.glob(os.path.join(STATE, "runs", "*.json"))):
        day = os.path.basename(f)[:10]
        if day not in wanted:
            continue
        try:
            r = json.load(open(f, encoding="utf-8-sig"))
        except Exception:
            continue
        if r.get("dry_run"):
            continue
        for c in (r.get("created") or []):
            if c.get("dry_run"):
                continue
            e = (c.get("email") or "").strip().lower()
            if e:
                out[e] = min(out.get(e, "9999"), day)
    return out


def anchor_for(g, msgs, lead, exclude_id=None):
    """Newest message that can carry a reply, preferring our own outbound to this lead."""
    usable = [m for m in msgs if m.get("message_id_hdr") and m.get("id") != exclude_id]
    if not usable:
        return None, None
    lead_c = classify.canon(lead)
    mine = [m for m in usable
            if m.get("sender") in R.ME and classify.canon(m.get("to") or "") == lead_c]
    last = (mine or usable)[-1]
    subj = last.get("subject") or ""
    if not subj.strip():
        return None, None
    return {"subject": subj, "message_id_hdr": last["message_id_hdr"]}, last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domains", required=True,
                    help="comma-separated, e.g. swiggy.in,flipkart.com. Deliberately "
                         "required: this repairs a named slice, never the whole corpus.")
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    doms = {d.strip().lower().lstrip("@") for d in args.domains.split(",") if d.strip()}
    if not doms:
        log("ABORT: no domains given")
        return 5

    missing = [k for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")
               if not os.environ.get(k)]
    if missing:
        log(f"ABORT: missing secrets {missing}")
        return 4
    if not os.path.exists(RESUME_PATH):
        log(f"ABORT: resume not found at {RESUME_PATH}")
        return 4

    # Before the mailbox is opened: if a gate cannot read its source, nothing runs.
    try:
        barred, noted = load_holds()
        dnm_addrs, domain_bars = load_suppression()
    except RuntimeError as e:
        log(f"ABORT: {e}")
        return 4
    addr_bars = merge_bars(barred, dnm_addrs)
    log(f"suppression: {len(addr_bars)} addresses and {len(domain_bars)} domains barred "
        f"({len(barred)} from holds/{'/'.join(SUPPRESSING_BUCKETS)}, "
        f"{len(dnm_addrs)} from the registry and the promise ledgers)")

    g = Gmail()
    acct = g.profile_email()
    if acct != EXPECTED_ACCOUNT:
        log(f"ABORT: connected mailbox is {acct!r}, expected {EXPECTED_ACCOUNT!r}")
        return 2

    drafted = drafted_in_window(args.days)
    scope = {e: d for e, d in drafted.items() if e.split("@")[-1] in doms}
    log(f"mailbox verified: {acct}")
    log(f"{len(drafted)} drafted in the last {args.days}d; {len(scope)} in {sorted(doms)}")
    if not scope:
        log("nothing in scope")
        return 5

    report = {"date": datetime.date.today().isoformat(), "dry_run": args.dry_run,
              "domains": sorted(doms), "sent_ok": [], "rebuilt": [], "skipped": [],
              "barred": [], "notes": [], "errors": []}

    # THE GATE. Ahead of every Gmail read, every craft and every create, and applied on a
    # dry run too, because a dry run exists to show what a live run would do.
    scope, report["barred"] = gate_scope(scope, addr_bars, domain_bars)
    for b in report["barred"]:
        log(f"  BARRED  {b['to']}: {b['reason'][:110]}")
    for e in sorted(scope):
        nb = noted.get(classify.canon(e))
        if nb:
            report["notes"].append(
                {"to": e, "bucket": nb,
                 "note": f"in holds[{nb}], which does not bar this path"})
            log(f"  NOTE    {e}: in holds[{nb}], not a bar on this path")
    if not scope:
        log(f"nothing left in scope: all {len(report['barred'])} on holds.json")
        report["summary"] = {"in_scope": 0, "delivered": 0, "rebuilt": 0, "skipped": 0,
                             "barred": len(report["barred"]), "errors": 0}
        log(f"report -> {write_report(report)}")
        return 5

    existing = {}
    for d in g.list_drafts():
        existing.setdefault(d.get("threadId"), []).append(d["draft_id"])

    for lead, day in sorted(scope.items()):
        # THE TEST. A run report says a draft was created; only Sent says it left.
        try:
            hits = g.search_messages(f"in:sent to:{lead} after:{day.replace('-', '/')}", cap=5)
        except Exception as e:
            report["errors"].append(f"{lead}: sent check failed: {e}")
            log(f"  ERROR {lead}: {str(e)[:70]}")
            continue
        if hits:
            report["sent_ok"].append({"to": lead, "drafted": day})
            log(f"  OK      {lead}: delivered")
            continue

        log(f"  FAILED  {lead}: drafted {day}, nothing in Sent since")
        try:
            threads = g.search_messages(f"to:{lead}", cap=20)
        except Exception as e:
            report["errors"].append(f"{lead}: thread lookup failed: {e}")
            continue
        tid = threads[-1]["threadId"] if threads else None
        if not tid:
            report["skipped"].append({"to": lead, "reason": "no thread to reply in"})
            continue
        if existing.get(tid):
            report["skipped"].append({"to": lead, "reason": "a draft already exists there"})
            log(f"          skip: a draft already exists in that thread")
            continue
        try:
            msgs = g.thread_full(tid)
        except Exception as e:
            report["errors"].append(f"{lead}: thread unreadable: {e}")
            continue
        # The thread is now in hand, so the one check no ledger can keep current: did I
        # already tell this person I would stop writing? A ledger is a snapshot of the
        # last audit; this is the evidence itself.
        pledge = thread_promise(msgs, lead)
        if pledge:
            report["barred"].append({"to": lead, "drafted": day, "reason": pledge})
            log(f"          BARRED  {pledge[:110]}")
            continue

        anchor, last = anchor_for(g, msgs or [], lead)
        if not anchor:
            report["skipped"].append({"to": lead, "reason": "no threadable anchor"})
            continue

        first = classify.greeting_name(msgs, lead) if hasattr(classify, "greeting_name") else None
        if not first:
            report["skipped"].append({"to": lead, "reason": "no established first name"})
            log(f"          skip: cannot establish a first name")
            continue
        # Defence in depth. gate_scope already removed these, so arriving here means a
        # later edit bypassed it. That is a bug, logged as one, never a routine skip.
        why = suppressed_reason(lead, addr_bars, domain_bars)
        if why:
            report["errors"].append(f"{lead}: BARRED reached the craft step: {why[:90]}")
            log(f"          BUG: barred lead reached the craft step; refusing: {why[:80]}")
            continue

        org = lead.split("@")[-1].split(".")[0].title()
        month = datetime.date.fromisoformat(day).strftime("%B")
        built = template_craft.craft_template(lead, first, org, msgs, month)
        if not built["ok"]:
            report["skipped"].append({"to": lead,
                                      "reason": "template refused: " + (built["reasons"] or [""])[0][:60]})
            continue

        if args.dry_run:
            report["rebuilt"].append({"to": lead, "thread_id": tid, "draft_id": None,
                                      "dry_run": True})
            log(f"          WOULD REBUILD in {tid}")
            continue
        try:
            nd = g.create_reply_draft(lead, tid, anchor, built["body"])
        except Exception as e:
            report["errors"].append(f"{lead}: create failed: {e}")
            log(f"          ERROR creating: {str(e)[:70]}")
            continue
        did = (nd or {}).get("id")
        report["rebuilt"].append({"to": lead, "thread_id": tid, "draft_id": did})
        log(f"          REBUILT -> {did}")

    report["summary"] = {"in_scope": len(scope), "delivered": len(report["sent_ok"]),
                         "rebuilt": len(report["rebuilt"]),
                         "skipped": len(report["skipped"]),
                         "barred": len(report["barred"]),
                         "errors": len(report["errors"])}
    write_report(report)
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
