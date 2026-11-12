# -*- coding: utf-8 -*-
"""Paced cloud dispatch of queued drafts.

    python -m pipeline.run_dispatch [--dry-run] [--max N]

Replaces the manual Gmail-Android burst that wedged 84 messages in the Outbox on
2026-09-09. See pipeline/dispatch.py for the five invariants and the root-cause note.

SAFETY POSTURE: this is the only component in the repo that DELIVERS MAIL. It therefore
ships doubly disabled and fails closed:

  * DISPATCH_ENABLED must be exactly "true". Anything else is a no-op. There is no code
    path that sets it.
  * --dry-run is the DEFAULT. Real sending requires DISPATCH_LIVE=true as well, so a
    single mis-set variable cannot start mailing people.
  * The identity gate from run_daily/run_guard is repeated here: if the connected mailbox
    is not the expected account, abort before touching anything. A send-as alias can fool
    search heuristics, so profile identity is checked directly.

Exit codes: 0 ok, 2 wrong mailbox, 3 halted (rate limit or ambiguous failure), 4 missing
secrets, 5 disabled.
"""
import argparse
import datetime
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import classify, dispatch as D
from pipeline.gmail_api import Gmail, EXPECTED_ACCOUNT

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state")

DAILY_CAP = int(os.environ.get("DISPATCH_DAILY_CAP", "120"))
MAX_PER_RUN = int(os.environ.get("DISPATCH_MAX_PER_RUN", "10"))
PACE_SECONDS = float(os.environ.get("DISPATCH_PACE_SECONDS", "25"))


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
    print(f"[{datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')}] {m}",
          flush=True)


def utc_date():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", default=None)
    ap.add_argument("--max", type=int, default=MAX_PER_RUN)
    ap.add_argument("--created-on", default="", dest="created_on",
                    help="restrict this run to drafts CREATED on this UTC date "
                         "(YYYY-MM-DD). Drains one day's output without touching the "
                         "backlog sitting behind it in the same folder.")
    ap.add_argument("--ignore-standdown", action="store_true", dest="ignore_standdown",
                    help="release an active post-rate-limit stand-down for THIS run. "
                         "Never set by any scheduled path. A fresh quota signal still "
                         "halts the run and re-arms the timer.")
    ap.add_argument("--inventory", action="store_true",
                    help="report every draft and why it is or is not sendable, send "
                         "nothing, and write state/inventory-<date>.json. Answers "
                         "'what would a drain actually do' before one is authorised.")
    ap.add_argument("--allow-ids", default="", dest="allow_ids",
                    help="comma-separated draft ids to treat as authorised for this run. "
                         "For drafts the OWNER built in the Gmail app (he attaches the CV "
                         "there, so they carry it) which this pipeline never recorded in "
                         "sent_log and would therefore skip as 'not authored'.")
    args = ap.parse_args()

    if args.created_on:
        # Fail here, loudly, rather than 200 lines into a job log. The owner writes dates
        # day-first everywhere else, and "18-09-2026" used to raise deep inside dispatch()
        # where the traceback was swallowed into a green run that had sent nothing.
        try:
            datetime.datetime.strptime(args.created_on, "%Y-%m-%d")
        except ValueError:
            log(f"ABORT: --created-on must be YYYY-MM-DD, got {args.created_on!r}")
            return 4

    if os.environ.get("DISPATCH_ENABLED", "").strip().lower() != "true":
        log("DISABLED: set repo variable DISPATCH_ENABLED=true to arm. Nothing done.")
        return 5

    # Dry run unless BOTH the enable flag and the live flag are set, and --dry-run absent.
    live = os.environ.get("DISPATCH_LIVE", "").strip().lower() == "true"
    dry = True if args.dry_run else (not live)
    if dry:
        log("DRY RUN: no mail will be sent (set DISPATCH_LIVE=true for real sending)")

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

    # Holds are read FRESH on every run, never cached, because the whole point is to catch
    # leads held AFTER their draft was created. Same four buckets run_daily unions at
    # run_daily.py:245. Canonicalised so a dot-variant or +tag cannot slip past.
    holds = jload("holds.json", {})
    held = set()
    for bucket in ("replied", "bounced", "capped", "closed"):
        for e in (holds.get(bucket) or []):
            if isinstance(e, str):
                held.add(classify.canon(e))
    # "review" is a dict keyed by address, not a list.
    if isinstance(holds.get("review"), dict):
        for e in holds["review"]:
            held.add(classify.canon(e))
    log(f"holds: {len(held)} addresses barred from sending")

    # AUTHORSHIP. run_daily records the id of every draft it creates, so this is the only
    # trustworthy answer to "did we write this?". The recipient set cannot answer it: it
    # lists everyone we MAY mail. Without this, a half-finished personal reply the owner
    # parked in the folder, addressed to any tracked lead, is delivered by the cron.
    sent_rows = jload("sent_log.json", [])
    authored = {r["draft_id"] for r in sent_rows if r.get("draft_id")}
    # Explicitly allowed ids. The owner sometimes rebuilds a draft inside the Gmail app to
    # attach the CV; the app writes a NEW draft id that sent_log never saw, so the
    # authorship check would refuse to send exactly the drafts that DO carry the resume.
    # Passing ids here is a deliberate, logged act by a human, never inferred.
    extra = {x.strip() for x in (args.allow_ids or "").split(",") if x.strip()}
    if extra:
        # EXCLUSIVE, not additive. The first cut of this flag merely ADDED the ids to the
        # authored set, so the run still walked the folder in its own order and spent the
        # whole budget on pipeline drafts that happened to come first. Seven people got a
        # mail nobody chose to send them. When a human names ids, those ids are the run.
        log(f"allow-ids: restricting this run to {len(extra)} explicitly named draft ids")
        authored = extra
    log(f"authored: {len(authored)} drafts are eligible")

    # Touches recorded OUTSIDE this pipeline, and ONLY those.
    #
    # The comment this replaces said "every sent_log row is source 'local-wave-batch1',
    # i.e. a different tool mailing the same people". That was true when the file held 196
    # rows. It holds 1,834 now, and 1,639 of them are the pipeline's OWN drafts, which
    # become messages in the very thread dispatch is about to count. Adding them again is
    # double counting, and it is not theoretical: on 2026-09-18 it read contact1@amazon.com and
    # nandini.deshpande@amazon.com as 6/6 and PERMANENTLY TRASHED their drafts. Both have
    # exactly FOUR outbound in Gmail. 33 drafts were purged that morning on this arithmetic.
    #
    # Only rows from other tooling count here. A row this pipeline wrote is either already
    # in the thread, in which case the in-thread count has it, or it was never sent, in
    # which case it is not a touch at all.
    OUTSIDE_SOURCES = ("local-wave-batch1",)
    extra_touches = {}
    for r in sent_rows:
        if (r.get("source") or "") not in OUTSIDE_SOURCES:
            continue
        c = classify.canon(r.get("email") or "")
        if c:
            extra_touches[c] = extra_touches.get(c, 0) + 1
    if not held:
        # holds.json currently carries 100+ entries. An empty set almost certainly means
        # the file failed to load, and proceeding would silently drop the strongest guard.
        log("ABORT: holds set is empty, which is not a state this account should be in. "
            "Refusing to send rather than send unguarded.")
        return 4

    today = utc_date()
    if args.inventory:
        # Read-only. No counter is touched, no stand-down is written, nothing is sent.
        # The point is to see the population BEFORE authorising a drain over it, because
        # every expensive mistake this system has made was visible in the queue first and
        # nobody looked.
        inv = D.inventory(g, tracked, held=held, authored=authored,
                          extra_touches=extra_touches, created_on=args.created_on or None,
                          log=log)
        jsave(f"inventory-{today}.json", inv)
        log(json.dumps(inv["summary"], indent=1))
        return 0

    st = jload("dispatch.json", {})
    if st.get("date") != today:
        # Reset the DAILY COUNTER, never the stand-down. A 403 at 12:07Z writes a
        # halt 26h out, which by design lands on the following day; wiping it at the
        # 00:00Z rollover meant the very next slot resumed sending into an account that
        # was still refusing. The counter is per-day; the stand-down is not.
        st = {"date": today, "sent": 0, "halted_until": st.get("halted_until")}

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    standing_down = bool(st.get("halted_until")) and st["halted_until"] > now_iso
    if standing_down and args.ignore_standdown:
        # A 26-hour timer is a blunt instrument. It is armed by ANY quota signal and it
        # cannot tell a per-minute burst limit, which clears in minutes, from a daily
        # quota, which clears at 07:30 UTC. Releasing it is therefore sometimes correct
        # and always a judgement call, so it is a human act that says so out loud and
        # leaves a line in the log, never a quiet edit to state/dispatch.json.
        #
        # The release does NOT disarm the protection: a fresh quota signal still halts
        # this run and re-arms the timer 26 hours out. What it buys is the chance to find
        # out whether the account is still refusing, which the timer alone never asks.
        log(f"STAND-DOWN OVERRIDDEN by an explicit human flag. It ran to "
            f"{st['halted_until']}. Pace {PACE_SECONDS}s, max {args.max} this run. "
            f"A new quota signal will halt immediately and re-arm.")
        standing_down = False
    if standing_down:
        log(f"STAND-DOWN in effect until {st['halted_until']} (previous rate limit)")
        return 3

    # Owned HERE, handed to dispatch(), and read in the finally block. A job cancelled or
    # timed out mid-drain unwinds past dispatch()'s return, so binding `report` to that
    # return value meant an interrupted run recorded 0 sends however many it had made.
    # The counter then did not move, the daily cap stopped binding, and the next slot was
    # free to send a second full day's worth into an account that had already had one.
    report, errors = {"sent": 0}, []
    try:
        _r, errors = D.dispatch(
            g, tracked, held=held, authored=authored, report=report,
            extra_touches=extra_touches,
            sent_today=st.get("sent", 0),
            daily_cap=DAILY_CAP,
            max_per_run=args.max,
            pace_seconds=PACE_SECONDS,
            dry_run=dry,
            created_on=args.created_on or None,
            log=log,
        )
    except BaseException:
        # BaseException, not Exception: GitHub cancels a job or hits timeout-minutes with
        # SIGINT, which surfaces as KeyboardInterrupt and is NOT an Exception. Letting it
        # escape skipped the finally-block persistence below, so a run that had already
        # delivered real messages recorded zero of them and the daily cap stopped binding.
        traceback.print_exc()
        errors.append("dispatch raised")
    finally:
        # Persist the counter even on a crash. If this is skipped the daily cap silently
        # stops binding, which is the failure that makes every other guard pointless.
        if not dry:
            st["sent"] = st.get("sent", 0) + report.get("sent", 0)
            # ONLY a quota refusal stands the account down. Previously ANY halt did, and
            # three of the halts dispatch can report are perfectly benign: reaching the
            # daily cap, an ambiguous 502, and now an interrupt. A run that simply spent
            # its allowance would therefore bar the account until the next afternoon, and
            # the log would call it a rate limit. The 2026-09-17 stand-down that blocked
            # every send on 09-18 has exactly that shape: the run ended at its cap.
            if report.get("quota"):
                until = datetime.datetime.now(datetime.timezone.utc) + \
                        datetime.timedelta(hours=26)
                st["halted_until"] = until.isoformat()
                st["halted_reason"] = str(report.get("halted"))[:300]
                st["halted_armed_at"] = datetime.datetime.now(
                    datetime.timezone.utc).isoformat()
            elif report.get("halted"):
                log(f"halt was not a quota refusal, no stand-down armed: "
                    f"{report['halted']}")
            jsave("dispatch.json", st)

    log(json.dumps({k: report.get(k) for k in
                    ("attempted", "sent", "skipped", "rescued", "halted")}))
    if report.get("ambiguous"):
        log("AMBIGUOUS SENDS -- check Sent folder before re-running:")
        for a in report["ambiguous"]:
            log(f"   {a['email']}  draft={a['draft_id']}  {a['error']}")
    for e in errors:
        log(f"error: {e}")

    if errors and not report.get("halted"):
        # An exception escaping dispatch() was reported as exit 0, so a malformed
        # --created-on, or a crash after real messages had gone out, showed GitHub a green
        # tick and "sent today: 0". The documented exit codes have no value meaning
        # "crashed", and calling a crash "ok" is how a drain that delivered nothing gets
        # mistaken for a drain that had nothing to deliver.
        log(f"::error::dispatch did not complete: {errors[0]}")
        return 1
    return 3 if report.get("halted") else 0


if __name__ == "__main__":
    sys.exit(main())
