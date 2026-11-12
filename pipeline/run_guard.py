# -*- coding: utf-8 -*-
"""Standalone pre-burst queue guard.

Runs shortly before the mailbox's daily send burst and clears any queued draft whose
recipient has replied since it was created. Cheap: Gmail reads only, no model calls.

    python -m pipeline.run_guard [--dry-run]
"""
import argparse, datetime, json, os, sys, traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import classify, queue_guard
from pipeline.gmail_api import Gmail, EXPECTED_ACCOUNT

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state")


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


def log(m):
    print(f"[{datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')}] {m}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
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
    log(f"mailbox verified: {acct}")

    tracked = {classify.canon(p["email"]) for p in jload("pool.json", [])}
    tracked |= {classify.canon(r["email"]) for r in jload("sent_log.json", [])}
    log(f"tracking {len(tracked)} addresses")

    rescued, errors = queue_guard.sweep(g, tracked, dry_run=args.dry_run, log=log)

    if rescued and not args.dry_run:
        holds = jload("holds.json", {"replied": [], "bounced": [], "capped": [],
                                     "closed": [], "review": {}})
        holds.setdefault("replied", [])
        holds["replied"] = sorted(set(holds["replied"]) | {r["email"] for r in rescued})
        jsave("holds.json", holds)
        pool = [p for p in jload("pool.json", [])
                if classify.canon(p["email"]) not in {classify.canon(r["email"]) for r in rescued}]
        jsave("pool.json", pool)

    today = datetime.date.today().isoformat()
    jsave(f"runs/{today}-guard.json",
          {"date": today, "dry_run": args.dry_run, "rescued": rescued, "errors": errors})
    log(f"done: {len(rescued)} rescued, {len(errors)} errors")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
