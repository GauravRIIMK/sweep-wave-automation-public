# -*- coding: utf-8 -*-
"""Three-month census: every human mailed, how many times, and who is owed a follow-up.

    python -m pipeline.census [--days 95] [--add-to-pool]

WHY THIS IS NOT THE POOL BACKFILL
---------------------------------
backfill_touches fixes the 336 rows currently in the pool. The pool is the ACTIONABLE
set: discover() only admits threads it finds in `in:sent`, and recycle() only returns
leads that are quiet, under cap and unheld. Someone cold-mailed once in July who then
fell out of the pool is invisible to it, and they are exactly the population the owner
asked about, because nobody ever followed up with them.

METHOD: walk THREADS, not messages. The first version got this wrong.
----------------------------------------------------------------------
v1 fetched metadata for every sent message, one call each. Gmail paces at about 0.92s
per call, so 6,000 messages took 105 minutes and still hit the cap: the window holds
more than that, and the run silently measured only the newest slice.

The fix is to notice what the work is proportional to. A thread holds several mails to
the same person, so there are roughly four times fewer threads than messages, and
thread_full returns EVERY message in one call with senders and recipients intact. The
walk is therefore O(threads), and it yields exact per-person counts inside the window
instead of a truncated sample. Counting is still per MESSAGE within each thread, which
is the part sent_log got wrong.

One targeted step remains. An address whose only in-window mail is a single message may
still have older mail outside the window, so its all-time count is verified with one
search, and only for those candidates.

Output is state/census-<date>.json plus a printed summary. Nothing is mailed. With
--add-to-pool the actionable cold-mail-only leads are appended to pool.json so the
ranking rule can reach them; without it, this is read-only.

Exit codes: 0 ok, 2 wrong mailbox, 4 missing secrets.
"""
import argparse
import collections
import datetime
import email.utils
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import classify
from pipeline import registries as R
from pipeline.gmail_api import Gmail, EXPECTED_ACCOUNT

MINE = {classify.canon(a) for a in R.ME}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state")


def log(m):
    print(f"[{datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')}] {m}",
          flush=True)


def jload(name, default):
    p = os.path.join(STATE, name)
    if not os.path.exists(p):
        return default
    try:
        return json.load(open(p, encoding="utf-8-sig"))
    except Exception:
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=95, help="window, default ~3 months")
    ap.add_argument("--add-to-pool", action="store_true",
                    help="append actionable cold-mail-only leads to pool.json")
    ap.add_argument("--cap", type=int, default=30000,
                    help="max sent messages to LIST; thread ids are derived from these")
    args = ap.parse_args()

    missing = [k for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")
               if not os.environ.get(k)]
    if missing:
        log(f"ABORT: missing secrets {missing}")
        return 4

    g = Gmail()
    acct = g.profile_email()
    if acct != EXPECTED_ACCOUNT:
        log(f"ABORT: connected mailbox is {acct!r}, expected {EXPECTED_ACCOUNT!r}")
        return 2
    log(f"mailbox verified: {acct}; window {args.days} days")

    msgs = g.search_messages(f"in:sent newer_than:{args.days}d", cap=args.cap)
    tids = []
    seen_t = set()
    for m in msgs:
        t = m.get("threadId")
        if t and t not in seen_t:
            seen_t.add(t)
            tids.append(t)
    log(f"{len(msgs)} sent messages span {len(tids)} threads; walking the threads")
    if len(msgs) >= args.cap:
        log(f"::warning::the message listing hit the cap of {args.cap}; thread coverage "
            f"may be incomplete. Raise --cap to widen it.")

    counts = collections.Counter()
    first_seen, last_seen, threads = {}, {}, {}
    unreadable = 0
    for i, tid in enumerate(tids, 1):
        try:
            tmsgs = g.thread_full(tid)
        except Exception:
            unreadable += 1
            continue
        if not tmsgs:
            unreadable += 1
            continue
        for m in tmsgs:
            # OUR outbound only. A thread holds their replies too, and counting those
            # would inflate the touch count with mail we did not send.
            if m.get("sender") not in R.ME:
                continue
            when = datetime.datetime.fromtimestamp(
                (m.get("date_ms") or 0) / 1000,
                datetime.timezone.utc).strftime("%Y-%m-%d")
            for _, addr in email.utils.getaddresses([m.get("to") or ""]):
                a = (addr or "").strip().lower()
                if not a or "@" not in a:
                    continue
                k = classify.canon(a)
                if k in MINE:
                    continue        # he mails himself links; that is not a lead
                counts[k] += 1
                threads.setdefault(k, tid)
                if k not in first_seen or when < first_seen[k]:
                    first_seen[k] = when
                last_seen[k] = max(last_seen.get(k, ""), when)
        if i % 100 == 0:
            log(f"  {i}/{len(tids)} threads read, {len(counts)} distinct recipients")

    log(f"read {len(tids) - unreadable} threads ({unreadable} unreadable), "
        f"{len(counts)} distinct recipients")

    # An address with one mail IN THE WINDOW may have older mail outside it. Verify only
    # those candidates, which is a few hundred searches rather than thirteen hundred.
    cand = [a for a, n in counts.items() if n == 1]
    log(f"{len(cand)} look like cold-mail-only; verifying all-time counts")
    alltime = {}
    for i, a in enumerate(cand, 1):
        try:
            alltime[a] = len(g.search_messages(f"in:sent to:{a}", cap=30))
        except Exception:
            alltime[a] = 1
        if i % 100 == 0:
            log(f"  verified {i}/{len(cand)}")

    holds = jload("holds.json", {})
    held = set()
    for b in ("replied", "bounced", "capped", "closed"):
        held |= {classify.canon(x) for x in (holds.get(b) or []) if isinstance(x, str)}
    held |= {classify.canon(x) for x in (holds.get("review") or {})}
    pool = jload("pool.json", [])
    in_pool = {classify.canon(p.get("email") or "") for p in pool}

    cold, blocked, already, machine = [], 0, 0, 0
    for a in cand:
        if alltime.get(a, 1) != 1:
            continue                      # older mail exists: not a cold-only lead
        if classify.is_non_human(a) or classify.blocked_reason(a):
            machine += 1
            continue
        if a in held:
            blocked += 1
            continue
        if a in in_pool:
            already += 1
            continue
        cold.append({"email": a, "thread_id": threads.get(a),
                     "first": first_seen.get(a), "last": last_seen.get(a)})

    dist = collections.Counter()
    for a, n in counts.items():
        dist[min(n, 6)] += 1
    log("")
    log("OUTBOUND COUNT IN THE WINDOW, all recipients:")
    for n in sorted(dist):
        tag = "1 mail (cold only, in window)" if n == 1 else (f"{n}+ mails" if n == 6 else f"{n} mails")
        log(f"   {dist[n]:>5}  {tag}")
    log("")
    log(f"COLD MAIL ONLY, verified all-time, and ACTIONABLE: {len(cold)}")
    log(f"   excluded, already in the pool      : {already}")
    log(f"   excluded, held/bounced/capped/closed: {blocked}")
    log(f"   excluded, machine or banned address : {machine}")

    rpt = {"date": datetime.date.today().isoformat(), "window_days": args.days,
           "sent_messages": len(msgs), "distinct_recipients": len(counts),
           "distribution": {str(k): v for k, v in sorted(dist.items())},
           "cold_only_actionable": len(cold),
           "excluded": {"in_pool": already, "held": blocked, "machine_or_banned": machine},
           "leads": sorted(cold, key=lambda r: (r.get("last") or ""), reverse=True)}
    with open(os.path.join(STATE, f"census-{rpt['date']}.json"), "w", encoding="utf-8") as f:
        json.dump(rpt, f, ensure_ascii=False, indent=1)

    if args.add_to_pool and cold:
        added = 0
        for r in cold:
            if not r.get("thread_id"):
                continue
            pool.append({"email": r["email"], "thread_id": r["thread_id"],
                         "org": r["email"].split("@")[-1].split(".")[0].title(),
                         "source": "census", "last_touch": r.get("last"), "touches": 1})
            added += 1
        p = os.path.join(STATE, "pool.json")
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(pool, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        log(f"added {added} cold-mail-only leads to the pool (now {len(pool)})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
