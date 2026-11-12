# -*- coding: utf-8 -*-
"""Write the REAL outbound count onto every pool row, and report the distribution.

    python -m pipeline.backfill_touches [--dry-run] [--limit N]

WHY
---
rank() decides who gets written to first, and until 2026-09-24 its touch term came from
sent_log. sent_log holds rows this pipeline created. Most of this corpus was mailed by
other tooling months before the pipeline existed, so the term read 0 for people with
four real mails behind them, and they were ranked as if untouched.

The owner's rule is that a lead who got the cold mail and nothing after it comes first,
always. That rule cannot be applied at all without a true count, because "one mail, no
follow-up" and "four mails" were indistinguishable.

classify() derives the true count from the threads, and run_daily now caches it on the
pool row as it goes. That fixes the pool a few hundred rows at a time, as they are
examined. This module does the whole pool at once so the rule applies from the next run
rather than converging over a fortnight.

ONE SEARCH PER LEAD, `in:sent to:<addr>`, counting MESSAGES not threads: a single thread
usually holds several mails to the same person, so counting threads would undercount
exactly the way sent_log does.

Exit codes: 0 ok, 2 wrong mailbox, 4 missing secrets.
"""
import argparse
import collections
import datetime
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.gmail_api import Gmail, EXPECTED_ACCOUNT

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state")


def log(m):
    print(f"[{datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')}] {m}",
          flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    missing = [k for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")
               if not os.environ.get(k)]
    if missing:
        log(f"ABORT: missing secrets {missing}")
        return 4

    pool_path = os.path.join(STATE, "pool.json")
    pool = json.load(open(pool_path, encoding="utf-8-sig"))
    g = Gmail()
    acct = g.profile_email()
    if acct != EXPECTED_ACCOUNT:
        log(f"ABORT: connected mailbox is {acct!r}, expected {EXPECTED_ACCOUNT!r}")
        return 2
    log(f"mailbox verified: {acct}; {len(pool)} pool rows")

    dist, changed, errors = collections.Counter(), 0, 0
    rows = pool[:args.limit] if args.limit else pool
    for i, p in enumerate(rows, 1):
        addr = (p.get("email") or "").strip()
        if not addr:
            continue
        try:
            msgs = g.search_messages(f"in:sent to:{addr}", cap=60)
        except Exception as e:
            errors += 1
            log(f"  ERROR {addr}: {str(e)[:80]}")
            continue
        n = len(msgs)
        dist[n] += 1
        if p.get("touches") != n:
            changed += 1
        if not args.dry_run:
            p["touches"] = n
        if i % 50 == 0:
            log(f"  {i}/{len(rows)} scanned")

    log(f"scanned {sum(dist.values())}, updated {changed}, errors {errors}")
    log("outbound-count distribution across the pool:")
    for n in sorted(dist):
        label = ("NEVER mailed" if n == 0 else
                 "COLD MAIL ONLY, no follow-up  <-- these now rank first" if n == 1 else
                 f"{n} mails")
        log(f"   {dist[n]:>5}  {label}")
    only_cold = dist.get(1, 0)
    log(f"leads owed a first follow-up: {only_cold} "
        f"({100.0 * only_cold / max(1, sum(dist.values())):.0f}% of the pool)")

    if not args.dry_run:
        tmp = pool_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(pool, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, pool_path)
        log("pool.json written")

    rpt = os.path.join(STATE, f"touches-{datetime.date.today().isoformat()}.json")
    with open(rpt, "w", encoding="utf-8") as f:
        json.dump({"date": datetime.date.today().isoformat(),
                   "scanned": sum(dist.values()), "updated": changed, "errors": errors,
                   "distribution": {str(k): v for k, v in sorted(dist.items())},
                   "cold_mail_only": only_cold}, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
