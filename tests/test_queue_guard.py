# -*- coding: utf-8 -*-
"""Queue guard: rescue stale follow-ups, never touch a hand-written reply.

Run: python -m tests.test_queue_guard
"""
import datetime, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import queue_guard, classify

FAILS = []
DAY = 86400000
HOUR = 3600000
NOW = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
ME = "rathore.gaurav1007@gmail.com"


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  <-- {extra}"))
    if not cond:
        FAILS.append(name)


def msg(sender, ago_ms, to=""):
    return {"id": f"m{ago_ms}{sender}", "sender": sender, "to": to, "to_name": "",
            "date_ms": NOW - ago_ms, "subject": "s", "message_id_hdr": "<x>",
            "text": "body", "labels": [], "all_text": "body"}


class FG:
    def __init__(self, drafts, threads):
        self.drafts, self.threads, self.trashed = drafts, threads, []

    def list_drafts(self):
        return [{"draft_id": d["draft_id"], "msg_id": d["msg_id"], "threadId": d["threadId"]}
                for d in self.drafts]

    def draft_detail(self, did):
        return next((d for d in self.drafts if d["draft_id"] == did), None)

    def thread_full(self, tid):
        return self.threads.get(tid)

    def trash_message(self, mid):
        self.trashed.append(mid)
        return {}


def scenario(name, thread, draft_age_h, tracked=True, expect_rescue=True):
    drafts = [{"draft_id": "d1", "msg_id": "dm1", "threadId": "t1",
               "to": "trisha.hegde@acme.com", "internalDate": NOW - draft_age_h * HOUR}]
    g = FG(drafts, {"t1": thread})
    tr = {classify.canon("trisha.hegde@acme.com")} if tracked else set()
    rescued, errors = g and queue_guard.sweep(g, tr, log=lambda *_: None)
    got = len(rescued) == 1 and g.trashed == ["dm1"]
    check(name, got == expect_rescue, f"rescued={len(rescued)} trashed={g.trashed} errors={errors}")


print("\n== queue guard ==")

# 1. THE HARM: draft queued 30h ago, they replied 5h ago -> must be pulled back
scenario("stale follow-up rescued when they reply after it was queued",
         [msg(ME, 40 * HOUR, "trisha.hegde@acme.com"),
          msg("trisha.hegde@acme.com", 5 * HOUR)],
         draft_age_h=30, expect_rescue=True)

# 2. THE FALSE POSITIVE: a hand-written reply answers an OLDER inbound -> must survive
scenario("hand-written reply to an older inbound is NOT trashed",
         [msg(ME, 40 * HOUR, "trisha.hegde@acme.com"),
          msg("trisha.hegde@acme.com", 26 * HOUR)],
         draft_age_h=2, expect_rescue=False)

# 3. no reply at all
scenario("draft with no reply is left alone",
         [msg(ME, 40 * HOUR, "trisha.hegde@acme.com")],
         draft_age_h=30, expect_rescue=False)

# 4. machine "reply" must not count
scenario("auto-reply / bounce does not trigger a rescue",
         [msg(ME, 40 * HOUR, "trisha.hegde@acme.com"),
          msg("mailer-daemon@googlemail.com", 5 * HOUR)],
         draft_age_h=30, expect_rescue=False)

# 5. untracked recipient: never touch someone else's draft
scenario("draft to an untracked address is never touched",
         [msg(ME, 40 * HOUR, "trisha.hegde@acme.com"),
          msg("trisha.hegde@acme.com", 5 * HOUR)],
         draft_age_h=30, tracked=False, expect_rescue=False)

# 6. our own later message does not count as "they replied"
scenario("our own later message is not a reply",
         [msg(ME, 40 * HOUR, "trisha.hegde@acme.com"),
          msg(ME, 2 * HOUR, "trisha.hegde@acme.com")],
         draft_age_h=30, expect_rescue=False)

# 7. dry run reports without trashing
d = [{"draft_id": "d1", "msg_id": "dm1", "threadId": "t1",
      "to": "trisha.hegde@acme.com", "internalDate": NOW - 30 * HOUR}]
g = FG(d, {"t1": [msg(ME, 40 * HOUR, "trisha.hegde@acme.com"),
                  msg("trisha.hegde@acme.com", 5 * HOUR)]})
rescued, _ = queue_guard.sweep(g, {classify.canon("trisha.hegde@acme.com")},
                               dry_run=True, log=lambda *_: None)
check("dry run reports the rescue but trashes nothing",
      len(rescued) == 1 and g.trashed == [] and rescued[0]["trashed"] is False, g.trashed)

# 8. canonical identity: a dot-variant address is still recognised as tracked
d = [{"draft_id": "d1", "msg_id": "dm1", "threadId": "t1",
      "to": "nandinikamath@policybazaar.com", "internalDate": NOW - 30 * HOUR}]
g = FG(d, {"t1": [msg(ME, 40 * HOUR, "nandinikamath@policybazaar.com"),
                  msg("nandinikamath@policybazaar.com", 5 * HOUR)]})
rescued, _ = queue_guard.sweep(g, {classify.canon("varun.omkar@policybazaar.com")},
                               log=lambda *_: None)
check("dot-variant address is matched via canonical identity", len(rescued) == 1, rescued)

# 9. gap is reported for the human reading the summary
check("gap hours reported", rescued and rescued[0]["gap_hours"] > 0, rescued)

print(f"\n{'ALL QUEUE GUARD TESTS PASSED' if not FAILS else str(len(FAILS)) + ' FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
