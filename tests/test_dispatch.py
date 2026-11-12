# -*- coding: utf-8 -*-
"""Paced dispatch: every invariant tested against the failure it prevents.

Run: python -m tests.test_dispatch
"""
import datetime, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import classify, dispatch as D

FAILS = []
HOUR = 3600000
NOW = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
ME = "rathore.gaurav1007@gmail.com"


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  <-- {extra}"))
    if not cond:
        FAILS.append(name)


def msg(sender, ago_ms, to="", text="body"):
    return {"id": f"m{ago_ms}{sender}", "sender": sender, "to": to, "to_name": "",
            "date_ms": NOW - ago_ms, "subject": "s", "message_id_hdr": "<x>",
            "text": text, "labels": [], "all_text": text}


SIG = ("\n\nWarm regards,\nGaurav Rathore\n+91 7888939968"
       "\nLinkedIn: https://www.linkedin.com/in/gaurav1-grow-learn-together")
CLEAN_BODY = ("Hi Trisha,\n\nThank you for the time my earlier note took.\n\n"
              "I left Blinkit Bistro in July, so I am looking properly now.\n\n"
              "- Until July I ran growth across roughly 50 pods in four cities\n"
              "- At upGrad, a referral engine from zero to Rs 1.5 Cr in four months\n\n"
              "If a growth seat opens near you, I would value a pointer to whoever owns it."
              "\n\nThank you." + SIG)


class FG:
    """Fake Gmail. `fail_on` maps draft_id -> Exception to raise from send_draft."""

    def __init__(self, drafts, threads, fail_on=None, bodies=None):
        self.drafts, self.threads = drafts, threads
        self.fail_on = fail_on or {}
        self.bodies = bodies or {}
        self.sent = []

    def draft_body(self, did):
        # Default is doctrine-clean, so every pre-existing test keeps testing what it was
        # written to test rather than silently tripping the new send-time tone gate.
        return self.bodies.get(did, CLEAN_BODY)

    def attachments(self, msg_id):
        """Filenames on the message. Defaults to carrying the resume, because a sendable
        draft is the normal case and every older test was written assuming one."""
        d = next((x for x in self.drafts if x.get("msg_id") == msg_id), None)
        if d is None:
            return []
        return d.get("attachments", [RESUME])

    def has_resume(self, msg_id):
        return any(n.lower() == RESUME.lower() for n in self.attachments(msg_id))

    def list_drafts(self):
        return [{"draft_id": d["draft_id"], "msg_id": d["msg_id"], "threadId": d["threadId"]}
                for d in self.drafts]

    def draft_detail(self, did):
        return next((d for d in self.drafts if d["draft_id"] == did), None)

    def thread_full(self, tid):
        if tid == "BROKEN":
            raise RuntimeError("thread read failed")
        return self.threads.get(tid)

    def send_draft(self, did):
        if did in self.fail_on:
            raise self.fail_on[did]
        self.sent.append(did)
        return {"id": "sent-" + did}


# A real sendable draft carries the resume, so it is ~300KB. Fixtures say so explicitly:
# from 2026-09-17 dispatch refuses anything under MIN_SEND_BYTES, and it fails CLOSED on an
# unknown size, because a guard that read a missing field as 0 let seven body-only mails out.
WITH_RESUME = 310_000
RESUME = "Gaurav_Rathore_Resume.pdf"


def mkdrafts(n, age_h=2, addrs=None, size=WITH_RESUME):
    return [{"draft_id": f"d{i}", "msg_id": f"m{i}", "threadId": f"t{i}",
             "to": (addrs[i] if addrs else f"lead{i}@acme.com"),
             "sizeEstimate": size,
             "internalDate": NOW - age_h * HOUR}
            for i in range(n)]


def mkthreads(n, addrs=None):
    # Each thread: our outbound only, older than the draft, no promise. Nothing to block.
    return {f"t{i}": [msg(ME, 40 * HOUR, (addrs[i] if addrs else f"lead{i}@acme.com"))]
            for i in range(n)}


def tracked_for(n, addrs=None):
    return {classify.canon(addrs[i] if addrs else f"lead{i}@acme.com") for i in range(n)}


NOSLEEP = lambda _s: None
QUIET = lambda *_a, **_k: None
# Every draft the fakes create is d0..dN, so this stands in for "this pipeline authored
# them all". Tests that care about authorship override it explicitly.
ALLD = {f"d{i}" for i in range(100)}
GO = dict(dry_run=False, log=QUIET, sleep=NOSLEEP, held=set(), authored=ALLD)

# --- the resume guard -------------------------------------------------------------
bare1 = mkdrafts(1, size=2_000)
bare1[0]["attachments"] = []
g = FG(bare1, mkthreads(1))
rep, _ = D.dispatch(g, tracked_for(1), **GO)
check("a body-only draft (no resume) is NOT sent", g.sent == [], f"sent={g.sent}")
check("and it is reported as such",
      any("no resume" in x.get("reason", "") for x in rep["detail"]), rep["detail"])

unk = mkdrafts(1, size=0)
unk[0]["attachments"] = []          # unreadable part list comes back empty: refuse
g = FG(unk, mkthreads(1))
rep, _ = D.dispatch(g, tracked_for(1), **GO)
check("an UNKNOWN size fails closed", g.sent == [], f"sent={g.sent}")

print("\n== dispatch: recipient gates ==")

# --- INVARIANT 4: only tracked addresses -----------------------------------------
g = FG(mkdrafts(3), mkthreads(3))
rep, _ = D.dispatch(g, set(), **GO)
check("untracked drafts are never sent", g.sent == [] and rep["attempted"] == 0, f"sent={g.sent}")

# --- HOLDS: the defect this suite exists to prevent -------------------------------
# On 2026-09-10, 88 leads were retroactively found to have been promised silence and
# THREE of them already had drafts queued. A dispatcher that trusts creation-time gating
# mails all three. These are the real addresses.
HELD3 = ["aanya_chauhan@apple.com", "ishanieaswaran@morganstanley.com", "poojanaidu@pg.com"]
g = FG(mkdrafts(3, addrs=HELD3), mkthreads(3, addrs=HELD3))
rep, _ = D.dispatch(g, tracked_for(3, HELD3),
                    held={classify.canon(a) for a in HELD3}, authored=ALLD,
                    dry_run=False, log=QUIET, sleep=NOSLEEP)
check("a lead on holds.json is NEVER sent, even though it is tracked",
      g.sent == [] and rep["skipped"] == 3, f"sent={g.sent} rep={rep}")

# the same drafts WITHOUT the holds set must send, or the test above proves nothing
g = FG(mkdrafts(3, addrs=HELD3), mkthreads(3, addrs=HELD3))
rep, _ = D.dispatch(g, tracked_for(3, HELD3), **GO)
check("negative control: identical drafts DO send when not held", len(g.sent) == 3,
      f"sent={g.sent}")

# holds must survive dot/plus canonicalisation
DOTTED = ["aanya.chauhan@apple.com"]
g = FG(mkdrafts(1, addrs=DOTTED), mkthreads(1, addrs=DOTTED))
rep, _ = D.dispatch(g, tracked_for(1, DOTTED),
                    held={classify.canon("hemantdeshpande@apple.com")}, authored=ALLD,
                    dry_run=False, log=QUIET, sleep=NOSLEEP)
check("holds match through address canonicalisation (dots/+tags)", g.sent == [],
      f"sent={g.sent}")

# --- forgetting to pass holds must be loud, not silent ----------------------------
try:
    D.dispatch(FG(mkdrafts(1), mkthreads(1)), tracked_for(1), authored=ALLD,
               dry_run=False, log=QUIET, sleep=NOSLEEP)
    check("omitting `held` raises rather than sending unguarded", False, "no exception")
except ValueError:
    check("omitting `held` raises rather than sending unguarded", True)

# --- ORG / DOMAIN / ADDRESS BANS ---------------------------------------------------
for addr, label in [("someone@vedantu.com", "banned org"),
                    ("recruiter@greenhouse.io", "ATS domain"),
                    ("kritihegde@noon.com", "do-not-mail address")]:
    g = FG(mkdrafts(1, addrs=[addr]), mkthreads(1, addrs=[addr]))
    rep, _ = D.dispatch(g, tracked_for(1, [addr]), **GO)
    check(f"{label} is never sent ({addr})", g.sent == [], f"sent={g.sent}")

# the unilever/lever.co incident must not regress
g = FG(mkdrafts(1, addrs=["x@unilever.com"]), mkthreads(1, addrs=["x@unilever.com"]))
rep, _ = D.dispatch(g, tracked_for(1, ["x@unilever.com"]), **GO)
check("unilever.com is NOT caught by the lever.co ban", g.sent == ["d0"], f"sent={g.sent}")

print("\n== dispatch: authorship (the CRITICAL audit finding) ==")

# The invariant claimed "a draft this pipeline did not create is never sent", but the code
# only checked whether the RECIPIENT was tracked -- an allowlist of 1,799 addresses the
# pipeline may EVER mail, which says nothing about who wrote this particular draft. An
# adversarial audit reproduced the consequence against the real state files: a
# half-finished personal reply parked in the folder, addressed to a tracked lead, was
# delivered by the cron. queue_guard deliberately PRESERVES such drafts, so dispatch was
# sending exactly what the guard protects.
g = FG(mkdrafts(1), mkthreads(1))
rep, _ = D.dispatch(g, tracked_for(1), held=set(), authored=set(),
                    dry_run=False, log=QUIET, sleep=NOSLEEP)
check("a hand-written draft (not authored by us) is NEVER sent, even to a tracked lead",
      g.sent == [], f"sent={g.sent}")

g = FG(mkdrafts(2), mkthreads(2))
rep, _ = D.dispatch(g, tracked_for(2), held=set(), authored={"d1"}, max_per_run=2,
                    dry_run=False, log=QUIET, sleep=NOSLEEP)
check("only the draft we authored is sent; the other is skipped", g.sent == ["d1"],
      f"sent={g.sent}")

try:
    D.dispatch(FG(mkdrafts(1), mkthreads(1)), tracked_for(1), held=set(),
               dry_run=False, log=QUIET, sleep=NOSLEEP)
    check("omitting `authored` raises rather than sending unguarded", False, "no exception")
except ValueError:
    check("omitting `authored` raises rather than sending unguarded", True)

print("\n== dispatch: promise made AFTER the draft, and touch cap ==")

# The promise scan used to skip every message NEWER than the draft, in order to exclude
# the draft itself. That also discarded a close-loop mail the owner sent the next day
# through other tooling, so the queued follow-up shipped straight over the promise.
LATER_PROMISE = "This is my last note, I will stop here."
g = FG(mkdrafts(1, age_h=48),
       {"t0": [msg(ME, 72 * HOUR, "lead0@acme.com"),
               msg(ME, 5 * HOUR, "lead0@acme.com", LATER_PROMISE)]})
rep, _ = D.dispatch(g, tracked_for(1), **GO)
check("a promise of silence made AFTER the draft was queued blocks the send",
      g.sent == [], f"sent={g.sent}")

# ...but the draft's OWN text must still be excluded, now by id rather than timestamp
d = mkdrafts(1)
d[0]["msg_id"] = "SELF"
g = FG(d, {"t0": [{"id": "SELF", "sender": ME, "to": "lead0@acme.com", "to_name": "",
                   "date_ms": NOW - 2 * HOUR, "subject": "s", "message_id_hdr": "<x>",
                   "text": LATER_PROMISE, "labels": [], "all_text": LATER_PROMISE},
                  msg(ME, 40 * HOUR, "lead0@acme.com")]})
rep, _ = D.dispatch(g, tracked_for(1), **GO)
check("the draft's OWN text is excluded from the promise scan (by id, not timestamp)",
      g.sent == ["d0"], f"sent={g.sent}")

# TOUCH_CAP re-counted at send time. All 196 sent_log rows come from a tool outside this
# pipeline, so a lead at 5 touches when the draft was queued can be at 6 when it ships.
g = FG(mkdrafts(1), mkthreads(1))
rep, _ = D.dispatch(g, tracked_for(1),
                    extra_touches={classify.canon("lead0@acme.com"): 6}, **GO)
check("touch cap is re-applied at send time, counting out-of-band touches", g.sent == [],
      f"sent={g.sent}")

g = FG(mkdrafts(1), mkthreads(1))
rep, _ = D.dispatch(g, tracked_for(1),
                    extra_touches={classify.canon("lead0@acme.com"): 2}, **GO)
check("a lead under the cap still sends", g.sent == ["d0"], f"sent={g.sent}")

print("\n== dispatch: thread-derived gates ==")

# --- reply after the draft ---------------------------------------------------------
g = FG(mkdrafts(1), {"t0": [msg(ME, 40 * HOUR, "lead0@acme.com"),
                            msg("lead0@acme.com", 1 * HOUR)]})
rep, _ = D.dispatch(g, tracked_for(1), **GO)
check("draft is NOT sent when they replied after it was queued",
      g.sent == [] and rep["rescued"] == 1, f"sent={g.sent} rep={rep}")

# --- the false positive that check must NOT produce --------------------------------
g = FG(mkdrafts(1), {"t0": [msg(ME, 40 * HOUR, "lead0@acme.com"),
                            msg("lead0@acme.com", 26 * HOUR)]})
rep, _ = D.dispatch(g, tracked_for(1), **GO)
check("a reply OLDER than the draft does not block the send", g.sent == ["d0"], f"sent={g.sent}")

# --- PROMISE OF SILENCE ------------------------------------------------------------
PROMISE_TXT = "This is my final note on this, and I will stop here."
g = FG(mkdrafts(1), {"t0": [msg(ME, 40 * HOUR, "lead0@acme.com", PROMISE_TXT)]})
rep, _ = D.dispatch(g, tracked_for(1), **GO)
check("a promise of silence in an earlier mail blocks the send", g.sent == [], f"sent={g.sent}")

# a promise sitting only in QUOTED history (all_text) must NOT block: classify.py reads
# m["text"], the dequoted body, and dispatch must match it exactly
m_quoted = msg(ME, 40 * HOUR, "lead0@acme.com", "Just a normal note.")
m_quoted["all_text"] = "Just a normal note. > This is my final note and I will stop here."
g = FG(mkdrafts(1), {"t0": [m_quoted]})
rep, _ = D.dispatch(g, tracked_for(1), **GO)
check("a promise only in QUOTED history does not block", g.sent == ["d0"], f"sent={g.sent}")

# a promise from someone else, or to someone else, is not ours
g = FG(mkdrafts(1), {"t0": [msg("other@acme.com", 40 * HOUR, "lead0@acme.com", PROMISE_TXT)]})
rep, _ = D.dispatch(g, tracked_for(1), **GO)
check("a promise made by the OTHER party does not block us", g.sent == ["d0"], f"sent={g.sent}")

g = FG(mkdrafts(1), {"t0": [msg(ME, 40 * HOUR, "someoneelse@acme.com", PROMISE_TXT),
                            msg(ME, 41 * HOUR, "lead0@acme.com")]})
rep, _ = D.dispatch(g, tracked_for(1), **GO)
check("a promise addressed to a DIFFERENT recipient does not block", g.sent == ["d0"],
      f"sent={g.sent}")

# --- fails closed when the thread cannot be read -----------------------------------
g = FG([{"draft_id": "d0", "msg_id": "m0", "threadId": "BROKEN", "sizeEstimate": WITH_RESUME,
         "to": "lead0@acme.com", "internalDate": NOW - 2 * HOUR}], {})
rep, _ = D.dispatch(g, tracked_for(1), **GO)
check("unreadable thread fails CLOSED (does not send)", g.sent == [], f"sent={g.sent}")

g = FG(mkdrafts(1), {"t0": None})
rep, _ = D.dispatch(g, tracked_for(1), **GO)
check("thread_full returning None fails CLOSED", g.sent == [], f"sent={g.sent}")

print("\n== dispatch: tone doctrine re-checked at SEND time ==")

# Drafts already in the folder were crafted before the tone gate existed, and the doctrine
# keeps moving (the release tail was REQUIRED until 2026-08-26 and is BANNED now). A
# doctrine change must therefore apply to everything still UNSENT, not only to mail
# written after the change.
RELEASE = CLEAN_BODY.replace("Thank you.",
                             "If none of this is relevant, no reply is needed.\n\nThank you.", 1)
g = FG(mkdrafts(1), mkthreads(1), bodies={"d0": RELEASE})
rep, _ = D.dispatch(g, tracked_for(1), **GO)
check("a queued draft carrying a BANNED release tail is not sent", g.sent == [], f"sent={g.sent}")

DEBT = CLEAN_BODY.replace("Thank you.",
                          "I do not want to become a nuisance about this.\n\nThank you.", 1)
g = FG(mkdrafts(1), mkthreads(1), bodies={"d0": DEBT})
rep, _ = D.dispatch(g, tracked_for(1), **GO)
check("a queued draft with debt framing is not sent", g.sent == [], f"sent={g.sent}")

g = FG(mkdrafts(1), mkthreads(1), bodies={"d0": CLEAN_BODY})
rep, _ = D.dispatch(g, tracked_for(1), **GO)
check("negative control: a doctrine-clean draft IS sent", g.sent == ["d0"], f"sent={g.sent}")

g = FG(mkdrafts(1), mkthreads(1), bodies={"d0": ""})
rep, _ = D.dispatch(g, tracked_for(1), **GO)
check("an unreadable draft body fails CLOSED", g.sent == [], f"sent={g.sent}")

print("\n== dispatch: permanently-blocked drafts are purged ==")

class FGT(FG):
    def __init__(self, *a, **k):
        FG.__init__(self, *a, **k)
        self.trashed = []

    def trash_message(self, mid):
        self.trashed.append(mid)
        return {}

# A draft that can never be sent is not waiting for anything. Left in the folder it is
# still fired by hand (a draft here IS a send) and it eats a DAILY_CAP slot forever.
g = FGT(mkdrafts(1, addrs=["x@vedantu.com"]), mkthreads(1, addrs=["x@vedantu.com"]))
rep, _ = D.dispatch(g, tracked_for(1, ["x@vedantu.com"]), **GO)
check("a permanently blocked draft (banned org) is TRASHED, not left to rot",
      g.sent == [] and g.trashed == ["m0"] and rep["purged"] == 1,
      f"trashed={g.trashed} rep={rep}")

g = FGT(mkdrafts(1), mkthreads(1))
rep, _ = D.dispatch(g, tracked_for(1), held={classify.canon("lead0@acme.com")},
                    authored=ALLD, dry_run=False, log=QUIET, sleep=NOSLEEP)
check("a held lead's draft is purged", g.trashed == ["m0"], f"trashed={g.trashed}")

# a TEMPORARY block must NOT be purged: the copy can be rewritten, or they just replied
BAD = CLEAN_BODY.replace("Thank you.", "No reply is needed.\n\nThank you.", 1)
g = FGT(mkdrafts(1), mkthreads(1), bodies={"d0": BAD})
rep, _ = D.dispatch(g, tracked_for(1), **GO)
check("a TONE block is not purged (the copy can be fixed)",
      g.sent == [] and g.trashed == [], f"trashed={g.trashed}")

g = FGT(mkdrafts(1), {"t0": [msg(ME, 40 * HOUR, "lead0@acme.com"),
                             msg("lead0@acme.com", 1 * HOUR)]})
rep, _ = D.dispatch(g, tracked_for(1), **GO)
check("a draft they REPLIED to is not purged (queue_guard owns that)",
      g.trashed == [], f"trashed={g.trashed}")

# and a dry run must never destroy anything
g = FGT(mkdrafts(1, addrs=["x@vedantu.com"]), mkthreads(1, addrs=["x@vedantu.com"]))
rep, _ = D.dispatch(g, tracked_for(1, ["x@vedantu.com"]), held=set(), authored=ALLD,
                    dry_run=True, log=QUIET, sleep=NOSLEEP)
check("a DRY RUN purges nothing", g.trashed == [], f"trashed={g.trashed}")

print("\n== dispatch: rate, budget, failure ==")

# --- INVARIANT 2: a rate limit halts the WHOLE run ---------------------------------
err = RuntimeError("Gmail API 403: userRateLimitExceeded for quota metric 'Queries'")
g = FG(mkdrafts(5), mkthreads(5), fail_on={"d1": err})
rep, errs = D.dispatch(g, tracked_for(5), max_per_run=5, **GO)
check("rate limit HALTS the run", rep["halted"] is not None, f"rep={rep}")
check("rate limit stops BEFORE later drafts are attempted", g.sent == ["d0"],
      f"sent={g.sent} (d2..d4 must not be tried)")

# --- INVARIANT 5: an ambiguous failure halts and is surfaced -----------------------
g = FG(mkdrafts(4), mkthreads(4), fail_on={"d1": RuntimeError("502 Bad Gateway")})
rep, errs = D.dispatch(g, tracked_for(4), max_per_run=4, **GO)
check("ambiguous send failure halts the run", rep["halted"] is not None)
check("ambiguous send failure is recorded for a human",
      len(rep["ambiguous"]) == 1 and rep["ambiguous"][0]["email"] == "lead1@acme.com",
      f"ambiguous={rep['ambiguous']}")
check("ambiguous failure is NOT retried", g.sent == ["d0"], f"sent={g.sent}")

# --- quota string recognition ------------------------------------------------------
check("403 userRateLimitExceeded is a quota signal",
      D.is_quota_error(RuntimeError("403 userRateLimitExceeded")))
check("Apps Script 'Service invoked too many times' is a quota signal",
      D.is_quota_error(RuntimeError("Service invoked too many times for one day: email")))
check("Gmail 4.7.28 is a quota signal", D.is_quota_error(RuntimeError("smtp 4.7.28")))
check("a plain 502 is NOT a quota signal", not D.is_quota_error(RuntimeError("502 Bad Gateway")))

# --- budgets -----------------------------------------------------------------------
g = FG(mkdrafts(10), mkthreads(10))
rep, _ = D.dispatch(g, tracked_for(10), max_per_run=3, **GO)
check("max_per_run binds", len(g.sent) == 3, f"sent={len(g.sent)}")

g = FG(mkdrafts(10), mkthreads(10))
rep, _ = D.dispatch(g, tracked_for(10), sent_today=118, daily_cap=120, max_per_run=10, **GO)
check("daily cap binds below max_per_run", len(g.sent) == 2, f"sent={len(g.sent)}")

g = FG(mkdrafts(5), mkthreads(5))
rep, _ = D.dispatch(g, tracked_for(5), sent_today=120, daily_cap=120, **GO)
check("exhausted daily cap sends nothing", g.sent == [] and rep["halted"], f"rep={rep}")

# --- dry run -----------------------------------------------------------------------
g = FG(mkdrafts(5), mkthreads(5))
rep, _ = D.dispatch(g, tracked_for(5), max_per_run=5,
                    dry_run=True, log=QUIET, sleep=NOSLEEP, held=set(), authored=ALLD)
check("dry run sends nothing but still reports a plan",
      g.sent == [] and rep["sent"] == 5, f"sent={g.sent} rep={rep}")

# --- pacing ------------------------------------------------------------------------
slept = []
g = FG(mkdrafts(3), mkthreads(3))
D.dispatch(g, tracked_for(3), max_per_run=3, pace_seconds=25,
           dry_run=False, log=QUIET, sleep=lambda s: slept.append(s), held=set(), authored=ALLD)
check("pacing sleeps between sends (n-1 gaps)", slept == [25, 25], f"slept={slept}")


# --- a big draft is not a proof of an attachment (owner-reported, 2026-09-18) -------
# A reply quoting a long inbound reached 141,762 bytes carrying NOTHING, which is nearly
# three times the old 50,000 floor. The floor was also an eighth of what the resume weighs
# on the wire, so it never tested what it claimed to. Both directions asserted here.
fat_but_bare = mkdrafts(1, size=141_762)
fat_but_bare[0]["attachments"] = []
g = FG(fat_but_bare, mkthreads(1))
D.dispatch(g, tracked_for(1), **GO)
check("a LARGE draft with no attachment is still refused", g.sent == [], f"sent={g.sent}")

fat_wrong_file = mkdrafts(1, size=450_000)
fat_wrong_file[0]["attachments"] = ["JD_Swiggy_SeniorManager.pdf"]
g = FG(fat_wrong_file, mkthreads(1))
D.dispatch(g, tracked_for(1), **GO)
check("a draft carrying some OTHER pdf is refused", g.sent == [], f"sent={g.sent}")

small_but_right = mkdrafts(1, size=1_000)
small_but_right[0]["attachments"] = [RESUME]
g = FG(small_but_right, mkthreads(1))
D.dispatch(g, tracked_for(1), **GO)
check("the resume by NAME is what makes a draft sendable, not its size",
      g.sent == ["d0"], f"sent={g.sent}")

cased = mkdrafts(1)
cased[0]["attachments"] = ["gaurav_rathore_RESUME.PDF"]
g = FG(cased, mkthreads(1))
D.dispatch(g, tracked_for(1), **GO)
check("the filename match is case-insensitive", g.sent == ["d0"], f"sent={g.sent}")

multi = mkdrafts(1)
multi[0]["attachments"] = ["signature.png", RESUME]
g = FG(multi, mkthreads(1))
D.dispatch(g, tracked_for(1), **GO)
check("the resume is found among several attachments", g.sent == ["d0"], f"sent={g.sent}")

# --- day scoping: drain one day without touching the backlog behind it -------------
# The folder holds weeks of drafts. "Send what was written today" and "send the queue"
# are different requests, and answering the second when the first was asked mails people
# the owner never chose to mail on this pass.
DAY = "2026-09-18"
DAY_START, DAY_END = D.day_bounds_ms(DAY)
check("day_bounds_ms spans exactly 24h", DAY_END - DAY_START == 86_400_000,
      f"{DAY_END - DAY_START}")


def mkday(n, created_ms):
    ds = mkdrafts(n)
    for d in ds:
        d["internalDate"] = created_ms
    return ds


today_ds = mkday(3, DAY_START + 6 * HOUR)
old_ds = mkday(3, DAY_START - 6 * HOUR)
for i, d in enumerate(old_ds):
    d["draft_id"], d["msg_id"], d["threadId"] = f"o{i}", f"om{i}", f"ot{i}"
    d["to"] = f"old{i}@acme.com"
threads = mkthreads(3)
threads.update({f"ot{i}": [msg(ME, 40 * HOUR, f"old{i}@acme.com")] for i in range(3)})
tracked_both = tracked_for(3) | {classify.canon(f"old{i}@acme.com") for i in range(3)}
authored_both = ALLD | {f"o{i}" for i in range(3)}

g = FG(today_ds + old_ds, threads)
D.dispatch(g, tracked_both, max_per_run=10, dry_run=False, log=QUIET, sleep=NOSLEEP,
           held=set(), authored=authored_both, created_on=DAY)
check("created_on sends only that day's drafts",
      sorted(g.sent) == ["d0", "d1", "d2"], f"sent={g.sent}")

g = FG(today_ds + old_ds, threads)
D.dispatch(g, tracked_both, max_per_run=10, dry_run=False, log=QUIET, sleep=NOSLEEP,
           held=set(), authored=authored_both)
check("no created_on still drains the whole queue", len(g.sent) == 6, f"sent={g.sent}")

# A draft at 23:59:59.999 is IN; one at 00:00:00.000 next day is OUT. Gmail's own
# after:/before: are exclusive, which is exactly the off-by-one that silently drops a
# whole cohort, so both edges are asserted rather than assumed.
g = FG(mkday(1, DAY_END - 1), mkthreads(1))
D.dispatch(g, tracked_for(1), max_per_run=5, dry_run=False, log=QUIET, sleep=NOSLEEP,
           held=set(), authored=ALLD, created_on=DAY)
check("last millisecond of the day is inside the window", g.sent == ["d0"], f"{g.sent}")

g = FG(mkday(1, DAY_END), mkthreads(1))
D.dispatch(g, tracked_for(1), max_per_run=5, dry_run=False, log=QUIET, sleep=NOSLEEP,
           held=set(), authored=ALLD, created_on=DAY)
check("first millisecond of the next day is outside it", g.sent == [], f"{g.sent}")

# Scoping must NEVER widen what is sendable.
g = FG(mkday(1, DAY_START + HOUR), mkthreads(1))
D.dispatch(g, tracked_for(1), max_per_run=5, dry_run=False, log=QUIET, sleep=NOSLEEP,
           held={classify.canon("lead0@acme.com")}, authored=ALLD, created_on=DAY)
check("day scope does not override holds", g.sent == [], f"{g.sent}")

bare = mkday(1, DAY_START + HOUR)
bare[0]["sizeEstimate"] = 2_000
bare[0]["attachments"] = []
g = FG(bare, mkthreads(1))
D.dispatch(g, tracked_for(1), max_per_run=5, dry_run=False, log=QUIET, sleep=NOSLEEP,
           held=set(), authored=ALLD, created_on=DAY)
check("day scope does not override the resume guard", g.sent == [], f"{g.sent}")

# --- inventory: the same verdicts the sender reaches, and nothing sent --------------
mixed = mkday(2, DAY_START + HOUR)
mixed[1]["sizeEstimate"] = 2_000
mixed[1]["attachments"] = []
g = FG(mixed, mkthreads(2))
inv = D.inventory(g, tracked_for(2), held=set(), authored=ALLD, created_on=DAY, log=QUIET)
check("inventory sends nothing", g.sent == [], f"sent={g.sent}")
check("inventory counts one sendable of two", inv["summary"]["sendable"] == 1,
      str(inv["summary"]))
check("inventory names the bare draft's reason",
      any("no resume" in (r["reason"] or "") for r in inv["drafts"]),
      str([r["reason"] for r in inv["drafts"]]))
check("inventory flags has_resume per draft",
      [r["has_resume"] for r in inv["drafts"]] == [True, False],
      str([r["has_resume"] for r in inv["drafts"]]))
check("inventory groups sendable by domain",
      inv["summary"]["sendable_by_domain"] == {"acme.com": 1},
      str(inv["summary"].get("sendable_by_domain")))

# A report that disagrees with the sender is worse than no report, because it is believed.
g2 = FG(mixed, mkthreads(2))
D.dispatch(g2, tracked_for(2), max_per_run=10, dry_run=False, log=QUIET, sleep=NOSLEEP,
           held=set(), authored=ALLD, created_on=DAY)
check("inventory's sendable count equals what dispatch actually sends",
      inv["summary"]["sendable"] == len(g2.sent),
      f"{inv['summary']['sendable']} vs {g2.sent}")

g = FG(mixed, mkthreads(2))
inv_all = D.inventory(g, tracked_for(2), held=set(), authored=set(), created_on=DAY,
                      log=QUIET)
check("inventory reports unauthored drafts rather than hiding them",
      inv_all["summary"]["in_window"] == 2 and inv_all["summary"]["sendable"] == 0,
      str(inv_all["summary"]))



# --- the audit findings, 2026-09-18: each of these shipped and had to be caught ------

# A quota refusal on the READ path is not "this draft has no attachment". Swallowing it
# relabelled every remaining draft in a 198-mail drain as unattached and finished green.
class QuotaOnRead(FG):
    def __init__(self, *a, **k):
        self.fail_after = k.pop("fail_after", 1)
        super().__init__(*a, **k)
        self.reads = 0

    def has_resume(self, msg_id):
        self.reads += 1
        if self.reads > self.fail_after:
            raise RuntimeError("403 userRateLimitExceeded")
        return True


g = QuotaOnRead(mkdrafts(5), mkthreads(5), fail_after=1)
rep, errs = D.dispatch(g, tracked_for(5), max_per_run=5, dry_run=False, log=QUIET,
                       sleep=NOSLEEP, held=set(), authored=ALLD)
check("a quota error while READING halts the run", rep["halted"] is not None, str(rep))
check("that halt is flagged as a quota event", rep.get("quota") is True, str(rep))
check("it does not mislabel the rest as having no resume",
      not any("no resume" in (d.get("reason") or "") for d in rep["detail"]),
      str(rep["detail"]))
check("it stops rather than walking the whole queue", g.reads <= 2, f"reads={g.reads}")

# A NON-quota read failure still fails closed, because an unreadable message is not
# evidence of a good one.
class BrokenRead(FG):
    def has_resume(self, msg_id):
        raise RuntimeError("502 Bad Gateway")


g = BrokenRead(mkdrafts(2), mkthreads(2))
rep, _ = D.dispatch(g, tracked_for(2), max_per_run=2, dry_run=False, log=QUIET,
                    sleep=NOSLEEP, held=set(), authored=ALLD)
check("a non-quota read failure refuses the draft without halting",
      g.sent == [] and rep["halted"] is None and rep["skipped"] == 2, str(rep))

# Reaching the daily cap is NOT a rate limit. Treating it as one stood the account down
# for 26 hours after a run that had done exactly what it was asked to do.
g = FG(mkdrafts(3), mkthreads(3))
rep, _ = D.dispatch(g, tracked_for(3), max_per_run=5, sent_today=120, daily_cap=120,
                    dry_run=False, log=QUIET, sleep=NOSLEEP, held=set(), authored=ALLD)
check("daily cap reached is a halt", rep["halted"] is not None, str(rep))
check("daily cap reached is NOT a quota event", rep.get("quota") is False, str(rep))

g = FG(mkdrafts(2), mkthreads(2), fail_on={"d0": RuntimeError("502 Bad Gateway")})
rep, _ = D.dispatch(g, tracked_for(2), max_per_run=2, dry_run=False, log=QUIET,
                    sleep=NOSLEEP, held=set(), authored=ALLD)
check("an ambiguous 502 halts but is NOT a quota event",
      rep["halted"] is not None and rep.get("quota") is False, str(rep))

g = FG(mkdrafts(2), mkthreads(2),
       fail_on={"d0": RuntimeError("403 userRateLimitExceeded")})
rep, _ = D.dispatch(g, tracked_for(2), max_per_run=2, dry_run=False, log=QUIET,
                    sleep=NOSLEEP, held=set(), authored=ALLD)
check("a real send rate limit IS a quota event", rep.get("quota") is True, str(rep))

# An interrupt mid-drain must not erase what was already delivered. GitHub cancels a job
# with SIGINT, which unwinds past dispatch()'s return, so the caller kept its initial
# {"sent": 0}: the counter never moved and the daily cap stopped binding.
class DiesMidway(FG):
    def send_draft(self, did):
        if did == "d3":
            raise KeyboardInterrupt("job cancelled")
        return super().send_draft(did)


caller_report = {"sent": 0}
g = DiesMidway(mkdrafts(6), mkthreads(6))
try:
    D.dispatch(g, tracked_for(6), max_per_run=6, dry_run=False, log=QUIET, sleep=NOSLEEP,
               held=set(), authored=ALLD, report=caller_report)
except BaseException:
    pass
check("the caller's report survives an interrupt with the real count",
      caller_report.get("sent") == 3 and len(g.sent) == 3,
      f"report={caller_report.get('sent')} actually_sent={len(g.sent)}")

# day_bounds_ms must reject a day-first date rather than let it crash deep in a run.
for bad in ("18-09-2026", "2026/09/18", "today", ""):
    try:
        D.day_bounds_ms(bad)
        ok = False
    except (ValueError, TypeError):
        ok = True
    check(f"day_bounds_ms rejects {bad!r}", ok)

# the two copies of the quota-signal list must never drift
from pipeline import gmail_api as GA
check("gmail_api and dispatch agree on what a quota error looks like",
      set(GA.QUOTA_SIGNALS) == set(D.QUOTA_SIGNALS),
      f"{set(GA.QUOTA_SIGNALS) ^ set(D.QUOTA_SIGNALS)}")


print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
