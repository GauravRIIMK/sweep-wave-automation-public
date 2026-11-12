# -*- coding: utf-8 -*-
"""Daily orchestrator.

Order matters and is deliberate:
  identity gate -> capacity gate -> incremental discovery -> classify (thread = truth)
  -> prune replied/bounced -> research -> craft -> validate -> draft -> persist state.

Nothing is mailed before the gates pass, and the number of drafts created never exceeds
what the mailbox actually dispatches in a day, so the queue cannot grow stale.
"""
import argparse, datetime, json, os, re, sys, traceback
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import (llm, registries as R, classify, craft, research,
                      template_craft, queue_guard)
from pipeline.gmail_api import Gmail, EXPECTED_ACCOUNT

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state")
DAILY_CAP = int(os.environ.get("DAILY_CAP", "40"))        # drafts this run may CREATE
# How deep the unsent queue may get. A separate number from DAILY_CAP: the mailbox is
# drained by hand in bursts, and a burst much larger than ~150 is what trips Gmail's
# userRateLimitExceeded (see memory gmail-outbox-stuck-root-cause).
QUEUE_MAX = int(os.environ.get("QUEUE_MAX", "900"))
MIN_QUIET_DAYS = int(os.environ.get("MIN_QUIET_DAYS", "3"))
ABANDON_DAYS = int(os.environ.get("ABANDON_DAYS", "30"))  # older than this = never going to send
STALE_DAYS = int(os.environ.get("STALE_DAYS", "7"))       # older than this = the mail has drifted
# Research costs about nine tenths of the bill and is discarded two thirds of the time.
# Off by default since 2026-09-24; set RESEARCH_ENABLED=true to restore it.
RESEARCH_ENABLED = os.environ.get("RESEARCH_ENABLED", "").strip().lower() == "true"
# Deterministic, zero-API copy when the model cannot or will not produce any.
TEMPLATE_FALLBACK = os.environ.get("TEMPLATE_FALLBACK", "true").strip().lower() == "true"
RECYCLE_QUIET_DAYS = int(os.environ.get("RECYCLE_QUIET_DAYS", "7"))   # before a mailed lead returns
MONTHS = ["", "January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


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
    os.makedirs(os.path.dirname(p), exist_ok=True)   # state/runs/ is not tracked by git when empty
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def log(msg):
    print(f"[{datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


SUBDOMAIN_PREFIXES = ("in.", "external.", "corp.", "mail.", "email.", "careers.", "jobs.")


def org_label(domain, overrides, row_org=None):
    """Resolve the company name that will appear IN the mail and in the research query.

    Returns None when it cannot be resolved, and the caller then holds the lead for
    review. Title-casing a domain label is never acceptable here: it produced "In" for
    in.ey.com, "External" for external.swiggy.in and "Parthenon" for parthenon.ey.com,
    each of which would have been written into a mail as the recipient's employer."""
    if row_org:
        return row_org
    if domain in overrides:
        return overrides[domain]
    for p in SUBDOMAIN_PREFIXES:
        if domain.startswith(p):
            rest = domain[len(p):]
            if rest in overrides:
                return overrides[rest]
    parts = domain.split(".")
    if len(parts) > 2:
        registrable = ".".join(parts[-2:])
        if registrable in overrides:
            return overrides[registrable]
    base = parts[0]
    if len(parts) == 2 and len(base) >= 4 and base not in ("mail", "corp", "email"):
        return base[:1].upper() + base[1:]
    return None


# Errors that will fail EVERY remaining lead, not just this one. Billing exhaustion and
# auth failures are account-level and permanent until a human acts, so grinding through the
# rest of the pool just burns time and buries the real cause in a wall of per-lead noise.
# Observed 2026-09-11: the Anthropic balance ran dry mid-run and 114 leads each failed with
# `400 ... "Your credit balance is too low"`. Each was logged as a per-lead error, the run
# finished, and the job reported SUCCESS. A quarter of the day's work silently did not
# happen. Transient errors (429, 5xx, 529 overloaded, timeouts) stay per-lead.
# Two classes, because they behave differently and the difference is expensive.
#
# UNRECOVERABLE: a bad or revoked key cannot heal inside a run. Abort on the first one.
UNRECOVERABLE_API_MARKERS = (
    "authentication_error",
    "invalid x-api-key",
    "permission_error",
    "account is not active",
    " 401",
)
# INTERMITTENT: billing and quota errors LOOK terminal and are not. On 2026-09-11 this
# account raised "credit balance is too low" 114 times DURING a run that still created
# its full 300 drafts, interleaved with successes. On 2026-09-16 the same string appeared
# once at lead 24 and the old code broke the loop, which at a 250 floor is the difference
# between 23 drafts and 250. Require a streak before believing it.
INTERMITTENT_API_MARKERS = (
    "credit balance is too low",
    "insufficient_quota",
    " 403",
    "rate_limit",
    "overloaded",
)
FATAL_API_MARKERS = UNRECOVERABLE_API_MARKERS + INTERMITTENT_API_MARKERS
API_FAIL_STREAK = int(os.environ.get("API_FAIL_STREAK", "5"))


def is_unrecoverable_api_error(exc):
    m = str(exc).lower()
    return any(k in m for k in UNRECOVERABLE_API_MARKERS)


def is_fatal_api_error(exc):
    m = str(exc).lower()
    return any(k in m for k in FATAL_API_MARKERS)


def discover(g, pool, last_run_iso, done_addrs=(), done_threads=()):
    """Incrementally add newly-mailed threads to the candidate pool. Keeps the wave
    alive for leads first contacted AFTER this automation was built.

    Leads already mailed or held are excluded explicitly. Without that, every lead this
    pipeline mails would be rediscovered the next day (its thread is in `in:sent` and it
    is no longer in the pool), the pool would grow back to the full corpus, and the daily
    examine budget would be spent re-checking people we already wrote to."""
    if last_run_iso:
        d = datetime.date.fromisoformat(last_run_iso[:10])
        days = max(2, (datetime.date.today() - d).days + 2)
    else:
        days = 30
    days = min(days, 400)
    known_threads = {p.get("thread_id") for p in pool} | set(done_threads)
    # Compare CANONICALLY. done_addrs arrives canonicalised (see the caller) while
    # p["email"] and the To header are raw, so a dotted or +tagged variant of someone
    # already mailed never matched and earned a SECOND pool row -- the same human
    # entering the queue twice, which the one-thread-per-human rule exists to prevent.
    known_addrs = ({classify.canon(p["email"]) for p in pool}
                   | {classify.canon(a) for a in done_addrs})
    msgs = g.search_messages(f"in:sent newer_than:{days}d", cap=1500)
    # map thread -> a REAL message id we actually saw. Never assume threadId doubles as a
    # message id: that only holds while the thread's first message still exists.
    first_msg = {}
    for m in msgs:
        first_msg.setdefault(m["threadId"], m["id"])
    added = 0
    for tid, mid in first_msg.items():
        if tid in known_threads:
            continue
        meta = g.message_meta(mid)
        if not meta:
            continue
        to = re.findall(r"[\w.+-]+@[\w.-]+", meta["headers"].get("to", "") or "")
        lead = to[0].lower() if to else None
        if not lead or lead in R.ME or classify.blocked_reason(lead):
            continue
        if classify.canon(lead) in known_addrs:   # one thread per human, never a second row
            continue
        pool.append({"email": lead, "thread_id": tid, "source": "discovered"})
        known_addrs.add(classify.canon(lead))
        added += 1
    return added


def created_so_far(day):
    """Drafts already created TODAY, summed across the first run and every top-up.

    The floor is a property of the DAY, not of one job. A single run can fall short for
    reasons that are nobody fault in particular (a thin pool at rank time, craft holds, a
    runner timeout), so a later pass has to know the shortfall rather than assume zero and
    put another full cap on top of a cap already spent.
    """
    total, d = 0, os.path.join(STATE, "runs")
    if not os.path.isdir(d):
        return 0
    for fn in sorted(os.listdir(d)):
        if not (fn.startswith(day) and fn.endswith(".json")) or "noop" in fn:
            continue
        try:
            r = json.load(open(os.path.join(d, fn), encoding="utf-8"))
            total += int(r.get("summary", {}).get("created", 0) or 0)
        except Exception:
            continue
    return total


def report_name(day, top_up):
    """The first run of the day owns <date>.json; later passes never overwrite it.

    A SECOND non-top-up run on the same UTC day used to take that same name, and on
    2026-09-21 that cost a whole run. A forced manual batch and the scheduled one both
    landed on 09-21, both wrote state/runs/2026-09-21.json with different content, and
    the second one's `git pull --rebase` hit a content conflict it could not resolve. The
    push retried five times, failed, and the job went red with "THIS RUN'S STATE IS LOST".
    Nothing was wrong with the mail it had crafted; it simply could not record it.

    So the name is only claimed once. Anything after the first takes a numbered suffix,
    exactly as top-ups already did. Two runs can still diverge on pool/holds/meta, which
    is a real conflict about real state and should be resolved by a human, but they can
    no longer collide on a file that is just this run's own diary.
    """
    if not top_up:
        first = "runs/%s.json" % day
        if not os.path.exists(os.path.join(STATE, first)):
            return first
        for n in range(2, 50):
            cand = "runs/%s-run-%d.json" % (day, n)
            if not os.path.exists(os.path.join(STATE, cand)):
                return cand
        return "runs/%s-run-last.json" % day
    for n in range(1, 50):
        cand = "runs/%s-topup-%d.json" % (day, n)
        if not os.path.exists(os.path.join(STATE, cand)):
            return cand
    return "runs/%s-topup-last.json" % day


def recycle(pool, sent_log, holds, key, today):
    """Return quiet, under-capped humans from sent_log to the pool.

    discover() is a one-way drain BY DESIGN: it is handed every address this pipeline has
    ever mailed and skips them, so that a lead is not rediscovered the day after it is
    written to. The consequence nobody costed is that the pool only ever shrinks. On
    2026-09-16 it held 753 rows against a 250/day floor, which is under three days, while
    sent_log held 805 humans who had received exactly ONE touch against a cap of six. The
    reservoir was never missing, only unreachable.

    This adds a lead back only when it is quiet for RECYCLE_QUIET_DAYS, is in no holds
    bucket, and is under the touch cap by sent_log's own count. It decides nothing else:
    classify() still reads the real thread and still owns the cap, the quiet window, the
    promise-of-silence check and the reply check, so a recycled lead that should not be
    written to is skipped there at the cost of a Gmail read and no LLM call.
    """
    known = {key(p["email"]) for p in pool}
    blocked = {key(a) for a in (set(holds.get("replied", [])) | set(holds.get("bounced", []))
                                | set(holds.get("capped", [])) | set(holds.get("closed", [])))}
    last, touches, row = {}, defaultdict(int), {}
    for r in sent_log:
        k = key(r["email"])
        touches[k] += 1
        d = r.get("date", "")
        if d >= last.get(k, ""):
            last[k], row[k] = d, r
    added, reasons = 0, defaultdict(int)
    for k, d in sorted(last.items(), key=lambda kv: kv[1]):     # quietest first
        if k in known:
            reasons["already in pool"] += 1
            continue
        if k in blocked:
            reasons["held (replied/bounced/capped/closed)"] += 1
            continue
        if touches[k] >= R.TOUCH_CAP:
            reasons["at touch cap"] += 1
            continue
        try:
            age = (today - datetime.date.fromisoformat(d)).days
        except Exception:
            reasons["unparseable date"] += 1
            continue
        if age < RECYCLE_QUIET_DAYS:
            reasons[f"quiet <{RECYCLE_QUIET_DAYS}d"] += 1
            continue
        r = row[k]
        if not r.get("thread_id"):
            reasons["no thread id"] += 1
            continue
        pool.append({"email": r["email"].lower(), "thread_id": r["thread_id"],
                     "org": r.get("org"), "source": "recycled",
                     "last_touch": d, "prior_touches": touches[k]})
        added += 1
    return added, dict(reasons)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="classify and craft, create NO drafts")
    ap.add_argument("--limit", type=int, default=0, help="hard cap on drafts this run")
    ap.add_argument("--top-up", action="store_true", dest="top_up",
                    help="later pass of the same day: create only the shortfall against DAILY_CAP")
    ap.add_argument("--force", action="store_true",
                    help="ignore the once-a-day guard (manual catch-up after a short run)")
    args = ap.parse_args()

    today = datetime.date.today()
    already = created_so_far(today.isoformat())
    RPT = report_name(today.isoformat(), args.top_up)
    report = {"date": today.isoformat(), "dry_run": args.dry_run, "top_up": args.top_up,
              "created_before_this_pass": already, "created": [], "held": [],
              "reply_due": [], "skipped": defaultdict(int), "errors": [], "bounced": []}

    missing = [k for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN",
                           "ANTHROPIC_API_KEY") if not os.environ.get(k)]
    if missing:
        log(f"ABORT: missing secrets {missing}. See SETUP.md. Nothing was created.")
        return 4

    g = Gmail()

    # ---- GATE 1: identity. Deterministic, unlike search heuristics. ----
    acct = g.profile_email()
    if acct != EXPECTED_ACCOUNT:
        log(f"ABORT: connected mailbox is {acct!r}, expected {EXPECTED_ACCOUNT!r}")
        report["errors"].append(f"wrong mailbox: {acct}")
        jsave(RPT, report)
        return 2
    log(f"mailbox verified: {acct}")

    meta = jload("meta.json", {})
    if (meta.get("last_run_date") == today.isoformat() and not args.dry_run
            and not (args.top_up or args.force)):
        # A benign refusal, not a fault: exit 0 so the daily job does not show a red X
        # for correct behaviour. Genuine faults (wrong mailbox, stalled dispatcher,
        # missing secrets) still exit non-zero so they actually raise an alarm.
        log("SKIP: a run already completed today (one batch per day). Nothing to do.")
        # What this run actually cost, per call site. Written into the report because the
        # two call sites differ by roughly an order of magnitude and nobody could see it.
        try:
            report["api_usage"] = llm.usage_summary()
        except Exception as e:
            report["api_usage"] = {"error": str(e)[:120]}

        report["summary"] = {"created": 0, "note": "already ran today"}
        jsave(f"runs/{today.isoformat()}-noop.json", report)
        return 0

    # ---- GATE 2: capacity. The queue must never exceed one dispatcher cycle. ----
    drafts = g.list_drafts()
    pending = len(drafts)
    now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    detail = []
    # Every queued draft is read, not the first 400. This list is the ONLY defence against
    # writing to someone who already has an unsent draft, so truncating at a fixed 400
    # while the ceiling is 900 would quietly re-mail whoever sat past the cut.
    for d in drafts[:max(400, QUEUE_MAX + 100)]:
        dd = g.draft_detail(d["draft_id"])
        if dd:
            detail.append(dd)
    # QUEUE GUARD. Drafts wait for the mailbox's daily burst, so someone can reply while
    # their follow-up is still queued. Clear those before deciding anything else.
    tracked_pre = {classify.canon(p["email"]) for p in jload("pool.json", [])}
    tracked_pre |= {classify.canon(r["email"]) for r in jload("sent_log.json", [])}
    rescued, guard_errors = queue_guard.sweep(g, tracked_pre, dry_run=args.dry_run, log=log)
    report["rescued"] = rescued
    report["errors"].extend(guard_errors)
    if rescued:
        gone = {r["email"] for r in rescued if r["trashed"]}
        detail = [d for d in detail if d.get("to", "").lower() not in gone]
        pending = len(detail)

    # ABANDONED vs QUEUED. Drafts older than ABANDON_DAYS are not evidence of anything:
    # this mailbox carries long-dead drafts (some from weeks back) that whatever sends
    # mail simply never picks up. Counting them as "stuck" made the gate abort every day
    # forever, and counting them against capacity permanently ate the daily budget.
    abandoned = sum(1 for d in detail if d["internalDate"]
                    and (now_ms - d["internalDate"]) > ABANDON_DAYS * 86400 * 1000)
    queued = pending - abandoned
    log(f"pending drafts: {pending} (live queue {queued}, abandoned >{ABANDON_DAYS}d {abandoned})")

    # LIVENESS is measured by whether mail is actually LEAVING, not by draft age. If
    # nothing has been sent recently the queue really is stuck and must not be grown.
    try:
        recent_sent = len(g.search_messages("in:sent newer_than:2d", cap=500))
    except Exception as e:
        recent_sent = -1
        report["errors"].append(f"liveness probe failed: {e}")
    log(f"messages sent in the last 2 days: {recent_sent}")
    if recent_sent == 0:
        log("ABORT: nothing has left the mailbox in 2 days; the sender is stalled.")
        report["errors"].append("no sends in 2 days")
        jsave(RPT, report)
        return 3

    # DAILY_CAP caps what this run CREATES; QUEUE_MAX caps how deep the queue may get.
    # These were one number (allowed = DAILY_CAP - queued) and that had two failure modes,
    # both observed live. With DAILY_CAP 300 and 320 drafts waiting, 09-13, 09-14 and
    # 09-15 each created 0 AND reported reply_due 0, because classification sits behind
    # this gate: "0" meant "never looked". Then the moment the mailbox drained by hand,
    # the same formula authorised a 181-draft burst in one run.
    headroom = max(0, QUEUE_MAX - queued)
    want = max(0, DAILY_CAP - already) if args.top_up else DAILY_CAP
    allowed = min(want, headroom)
    if args.limit:
        allowed = min(allowed, args.limit)
    # STALENESS, reported loudly and never silently. Drafts between STALE_DAYS and
    # ABANDON_DAYS old are the honest signal that the mailbox is not being drained.
    # QUEUE_MAX is what actually stops the queue growing; this is what tells a human
    # the ageing is happening well before the ceiling does it for them.
    stale = sum(1 for d in detail if d["internalDate"]
                and STALE_DAYS * 86400 * 1000 < (now_ms - d["internalDate"])
                <= ABANDON_DAYS * 86400 * 1000)
    report["stale_queued"] = stale
    if stale:
        log(f"WARNING: {stale} queued drafts are older than {STALE_DAYS} days and still unsent")
    log(f"capacity: queue {queued}/{QUEUE_MAX}, floor {DAILY_CAP}/day, created today {already}, "
        f"may create {allowed} in this pass")
    if allowed < want:
        log(f"NOTE: capacity limits this pass to {allowed} of {want} wanted; the queue holds "
            f"{queued} unsent drafts against QUEUE_MAX {QUEUE_MAX}")

    pool = jload("pool.json", [])
    sent_log = jload("sent_log.json", [])
    holds = jload("holds.json", {"replied": [], "bounced": [], "capped": [], "review": {}})
    holds.setdefault("closed", [])
    if isinstance(holds.get("review"), list):     # migrate the old append-only shape
        holds["review"] = {}
    overrides = jload("org_names.json", {})
    aliases = jload("alias_map.json", {})         # {alias: canonical} for flast@/firstlast@
    # SIBLING DOMAINS. One company, two mail domains, one human with the same local part
    # at both: canon() cannot see that leapfinance.com and leapscholar.com are the same
    # person's inbox, so on 2026-09-16 Arnav Banerjee got two drafts in the same run, one of
    # them naming an off-lane opening. Collapsing the DOMAIN keeps different humans at the
    # same company separate (the local part still distinguishes them) while making one
    # human at two brands a single identity for dedup, touch counting and the day ledger.
    dom_aliases = jload("domain_aliases.json", {})

    def key(addr):
        c = classify.canon(aliases.get(classify.canon(addr), classify.canon(addr)))
        local, _, dom = c.partition("@")
        return f"{local}@{dom_aliases.get(dom, dom)}"

    touches_by_lead = defaultdict(int)
    for row in sent_log:
        touches_by_lead[key(row["email"])] += 1

    # De-duplicate the pool by CANONICAL identity. Dots and +tags do not create separate
    # mailboxes, so "varun.omkar@" and "nandinikamath@" are one human; keying on the raw
    # string let both survive and both get mailed in the same run.
    seen_key, deduped = set(), []
    for p in pool:
        p["email"] = p["email"].lower()
        k = key(p["email"])
        if k in seen_key:
            continue
        seen_key.add(k)
        deduped.append(p)
    if len(deduped) != len(pool):
        log(f"pool de-duplicated by canonical identity: {len(pool)} -> {len(deduped)}")
    pool = deduped

    mailed_before = {key(r["email"]) for r in sent_log}
    mailed_threads = {r.get("thread_id") for r in sent_log if r.get("thread_id")}
    held_addrs = {key(a) for a in
                  set(holds["replied"]) | set(holds["bounced"]) | set(holds["capped"]) | set(holds["closed"])}
    added = discover(g, pool, meta.get("last_run_date"),
                     done_addrs=mailed_before | held_addrs, done_threads=mailed_threads)
    log(f"discovery: +{added} new candidate threads (pool {len(pool)})")
    # Mailbox discovery yields 0 to 5 leads a day once the cold-start backfill is spent
    # (measured across the 09-10 to 09-16 runs), which cannot feed a 250/day floor for
    # even one day. The refill has to come from leads already mailed and still quiet.
    recycled, why = recycle(pool, sent_log, holds, key, today)
    report["recycled_into_pool"] = recycled
    log(f"recycled: +{recycled} previously mailed leads quiet for {RECYCLE_QUIET_DAYS}+ days "
        f"(pool {len(pool)}); not recycled: {why}")

    # ---- rank: never-mailed-recently first, fewer touches first, priority orgs first ----
    prio = {d: i for i, d in enumerate(jload("priority_orgs.json", []))}
    def rank(p):
        """Priority org, then FEWEST REAL TOUCHES, then the FRESHEST connection.

        Owner's rule, 2026-09-24: a lead that got the cold mail and nothing after it
        comes first, always, and among equals the recently contacted come before the
        long-dormant. The reasoning is that a first mail is at its most answerable while
        it is still recent; a follow-up three months later is a different, colder ask.

        TWO CORRECTIONS ARE BAKED IN HERE.

        1. The touch count now prefers p["touches"], the count classify() derives from
           the actual threads, and falls back to sent_log only when that is missing.
           sent_log holds rows this pipeline wrote, and most of this corpus was mailed by
           other tooling before the pipeline existed, so the old term read 0 for people
           with four real mails behind them and ranked them as untouched.

        2. The tiebreak is REVERSED. It used to be `last_touch` ascending, i.e. longest
           silence first, which is the opposite of the rule above: it worked through the
           stalest leads and left freshly contacted ones to go cold. Negated via a sort
           key that puts the most recent date first.
        """
        dom = p["email"].split("@")[-1]
        t = p.get("touches")
        if t is None:
            t = p.get("prior_touches")
        if t is None:
            t = touches_by_lead.get(key(p["email"]), 0)
        # invert the date string so "2026-09-24" sorts before "2026-07-01"
        fresh_first = "".join(chr(0x7E - ord(c)) for c in (p.get("last_touch") or ""))
        return (prio.get(dom, 999), t, fresh_first, p["email"])
    pool.sort(key=rank)

    done_addrs = set(held_addrs)
    # Defence in depth against double-mailing when state was lost (a failed git push, a
    # crash mid-loop). A created draft does NOT appear in threads.get until it dispatches,
    # so the thread alone cannot prove we already wrote to someone today. The pending-draft
    # list can, and it survives any local state loss because it lives in the mailbox.
    queued_addrs = {key(d["to"]) for d in detail if d.get("to")}
    queued_threads = {d["threadId"] for d in detail if d.get("threadId")}
    if queued_addrs:
        log(f"{len(queued_addrs)} addresses already have a queued draft; they are excluded")

    created = 0
    fatal_api = None
    api_streak = 0      # consecutive account-level API failures, reset by any success
    template_used = 0   # drafts written by the zero-API crafter rather than the model
    scan_only = False   # API declared dead: keep classifying, stop spending on LLM calls
    examined = 0
    budget = max(allowed * 4, 40)     # CRAFT budget: classify more than we can mail
    # SCAN budget, deliberately independent of capacity. Reply-due detection lives inside
    # this loop, so breaking on `allowed` switched off the one check that finds people
    # waiting on an answer, precisely on the days the queue was fullest. Scanning costs
    # Gmail reads only, never an LLM call, so it stays on even at zero capacity.
    scanned = 0
    scan_budget = max(allowed * 4, 60)

    for p in list(pool):
        if scanned >= scan_budget:
            break
        lead = p["email"].lower()
        k = key(lead)
        if k in done_addrs:
            pool.remove(p)
            continue
        if k in queued_addrs or p.get("thread_id") in queued_threads:
            report["skipped"]["already has a queued draft"] += 1
            continue
        try:
            msgs = g.thread_full(p["thread_id"])
        except Exception as e:
            report["errors"].append(f"{lead}: thread fetch {e}")
            continue
        if msgs is None:
            report["skipped"]["thread gone"] += 1
            pool.remove(p)
            continue
        scanned += 1
        for m in msgs:
            m["threadId"] = p["thread_id"]

        # CROSS-THREAD AGGREGATION. One human can own several threads (a cold mail, a
        # role-specific mail, a calendar thread). Judging only the pool's thread would
        # miss a promise of silence, a reply, or touches recorded elsewhere, and the
        # 6-touch cap is defined per HUMAN, not per thread.
        try:
            others = g.search_messages(f"to:{lead}", cap=200)
            extra_tids = {x["threadId"] for x in others if x["threadId"] != p["thread_id"]}
            # NEWEST first. Gmail thread ids are monotonic hex, so a plain ascending sort
            # kept the six OLDEST threads and silently discarded the newest ones -- the
            # exact opposite of what this aggregation is for. A promise of silence, a
            # reply, or a fresh touch is far likelier in a recent thread than in a cold
            # mail from four months ago. (19e3... = May, 19f6... = July, 1a08... = Sept.)
            for t2 in sorted(extra_tids, reverse=True)[:6]:
                m2 = g.thread_full(t2)
                if not m2:
                    continue
                for m in m2:
                    m["threadId"] = t2
                msgs.extend(m2)
            if extra_tids:
                msgs.sort(key=lambda z: z["date_ms"])
        except Exception as e:
            report["errors"].append(f"{lead}: cross-thread scan {e}")

        # Stamp every message, from whichever thread, exactly once. In IST, not UTC: the
        # month is a human-facing claim ("I wrote in July") that the recipient checks
        # against the date shown on the quoted message in their own client.
        for m in msgs:
            m["date_iso"] = datetime.datetime.fromtimestamp(
                m["date_ms"] / 1000, IST).strftime("%Y-%m-%d")

        v = classify.classify(lead, msgs, now_ms,
                              sent_log_touches=0, min_quiet_days=MIN_QUIET_DAYS)
        if v["verdict"] == "REPLY_DUE":
            report["reply_due"].append({"email": lead, "thread_id": p["thread_id"],
                                        "reason": v["reason"]})
            holds["replied"].append(lead)
            pool.remove(p)
            continue
        # Cache the REAL touch count on the pool row. classify() has just derived it from
        # the threads, it costs nothing to keep, and it is what rank() needs next run.
        # Without this the ranking reads sent_log, which undercounts everyone the
        # pipeline did not personally mail.
        if isinstance(v.get("touches"), int):
            p["touches"] = v["touches"]
        if v["verdict"] == "SKIP":
            report["skipped"][v["reason"][:60]] += 1
            # Prune on the machine-readable state, never by substring-matching the prose
            # reason. "already answered" matched none of the old substrings, so answered
            # leads stayed in the pool forever and, ranking first, starved the daily
            # budget so that genuinely eligible leads were never reached.
            st = v.get("state", "")
            if st == "bounced":
                holds["bounced"].append(lead)
                report["bounced"].append(lead)
            elif st in classify.TERMINAL:
                holds["closed" if st == "answered" else "capped"].append(lead)
            if st in classify.TERMINAL:
                pool.remove(p)
            continue
        # Capacity is checked HERE, after classification, not before it. A lead that is
        # past today's cap keeps its place in the pool and is simply not crafted.
        if created >= allowed or examined >= budget:
            report["skipped"]["at capacity (classified, not crafted)"] += 1
            continue
        # Same reasoning when the API is down. Classification, pruning, bounce detection
        # and reply-due are Gmail reads: they cost nothing at Anthropic and they are the
        # checks that matter most on a bad day. Only the spending stops.
        if scan_only and not TEMPLATE_FALLBACK:
            report["skipped"]["API unavailable (classified, not crafted)"] += 1
            continue
        examined += 1      # only leads that reach research/craft consume the budget

        dom = lead.split("@")[-1]
        org = org_label(dom, overrides, p.get("org"))
        if not org:
            rec = holds["review"].setdefault(lead, {"reasons": [], "count": 0})
            rec["reasons"] = [f"unresolved org for domain {dom}"]
            rec["count"] += 1
            rec["last_seen"] = today.isoformat()
            report["held"].append({"email": lead, "reasons": rec["reasons"]})
            pool.remove(p)
            continue
        first = v["first"]
        month = MONTHS[int(v["anchor"]["date_iso"][5:7])]

        # research_lead and craft both call external services. Unguarded, a single
        # Anthropic 529 or a network blip propagated out of this loop and killed the run
        # BEFORE the jsave block at the end, discarding the whole run's pool pruning,
        # holds and reply-due report -- while drafts already created for earlier leads
        # stayed in the mailbox with no sent_log row, so those people were mailed again
        # the next day. One lead failing must cost one lead, not the run.
        try:
            # LEVER 2, 2026-09-24: research is OFF by default.
            #
            # It was one Anthropic call per lead carrying server-side web search with
            # max_uses 6, and it was roughly nine tenths of the bill: the search fee is
            # $10/1000, but the results it injects are re-charged as input tokens on every
            # following turn of the same request. Across 1,524 recorded drafts it returned
            # confidence "low" 990 times, and craft._research_block() DISCARDS low
            # confidence outright. So two thirds of the most expensive call was paid for
            # and thrown away, and it ran before the craft validator, so every held lead
            # paid for research and produced nothing.
            #
            # What made it affordable to switch off is that these mails are now JOB
            # AGNOSTIC by instruction: open_roles is no longer passed to the prompt at all,
            # which was the only thing research fed. Set RESEARCH_ENABLED=true to restore.
            res = {}
            if scan_only:
                # The API is dead for this run. Deterministic copy costs nothing and keeps
                # the day productive instead of returning zero drafts, which is exactly
                # what happened on 19, 20 and 21 September when the balance ran out. No
                # Anthropic call is made on this path at all.
                if not TEMPLATE_FALLBACK:
                    report["skipped"]["API unavailable (classified, not crafted)"] += 1
                    continue
                out = template_craft.craft_template(lead, first, org, msgs, month)
                if not out["ok"]:
                    report["skipped"]["template refused: " + (out["reasons"] or [""])[0][:40]] += 1
                    continue
                template_used += 1
            else:
                if RESEARCH_ENABLED:
                    res = research.research_lead(lead, first, org,
                                                 v["anchor"].get("subject", ""))
                out = craft.craft(lead, first, org, msgs, res, month)
                if not out["ok"] and TEMPLATE_FALLBACK:
                    # The model produced copy the validator rejected, twice. Rather than
                    # lose the lead to a hold, fall back to copy that passes by
                    # construction. 6 of 11 recorded drafts needed the model's own retry,
                    # so this is a common path, not an exotic one.
                    t = template_craft.craft_template(lead, first, org, msgs, month)
                    if t["ok"]:
                        log(f"TEMPLATE {lead}: model draft rejected "
                            f"({(out['reasons'] or [''])[0][:60]}), using deterministic copy")
                        out = t
                        template_used += 1
        except Exception as e:
            report["errors"].append(f"{lead}: craft/research failed: {e}")
            if is_unrecoverable_api_error(e):
                # A key that is rejected will be rejected for every remaining lead.
                fatal_api = str(e)[:300]
                log(f"FATAL: the API key itself is being refused, stopping spend. {fatal_api}")
                scan_only = True
                continue
            if is_fatal_api_error(e):
                api_streak += 1
                log(f"API failure {api_streak}/{API_FAIL_STREAK} in a row: {str(e)[:160]}")
                if api_streak >= API_FAIL_STREAK:
                    fatal_api = str(e)[:300]
                    log(f"FATAL: {API_FAIL_STREAK} account-level API failures in a row, "
                        f"stopping spend for this run. {fatal_api}")
                    scan_only = True
                continue
            log(f"SKIP {lead}: craft/research failed ({e})")
            continue
        api_streak = 0      # a success means the account is answering after all
        if not out["ok"]:
            rec = holds["review"].setdefault(lead, {"reasons": [], "count": 0,
                                                    "first_seen": today.isoformat()})
            rec["reasons"] = out["reasons"]
            rec["count"] += 1
            rec["last_seen"] = today.isoformat()
            report["held"].append({"email": lead, "reasons": out["reasons"]})
            log(f"HOLD {lead}: {out['reasons'][:2]}")
            # Retire after three failed days so it stops costing two LLM calls every run.
            if rec["count"] >= 3:
                holds["closed"].append(lead)
                pool.remove(p)
                log(f"RETIRED {lead} after {rec['count']} failed craft attempts")
            continue

        if args.dry_run:
            # A dry run exists to be READ. Keep the whole mail, plus what the research
            # actually returned, so a human can judge both before anything is sent.
            report["created"].append({"email": lead, "org": org, "dry_run": True,
                                      "confidence": res.get("confidence"),
                                      "research": {k: res.get(k) for k in
                                                   ("match_evidence", "title", "team",
                                                    "org_context", "hook")},
                                      "attempts": out["attempts"],
                                      "body": out["body"]})
            created += 1
            continue

        # Reply in the thread that actually holds the most recent outbound, which after
        # cross-thread aggregation may not be the pool's thread.
        reply_tid = v["anchor"].get("threadId") or p["thread_id"]
        try:
            d = g.create_reply_draft(lead, reply_tid, v["anchor"], out["body"])
        except Exception as e:
            report["errors"].append(f"{lead}: draft {e}")
            log(f"ERROR draft {lead}: {e}")
            continue
        created += 1
        sent_log.append({"email": lead, "org": org, "date": today.isoformat(),
                         "thread_id": reply_tid, "draft_id": (d or {}).get("id"),
                         "research": res.get("confidence"), "attempts": out["attempts"]})
        report["created"].append({"email": lead, "org": org,
                                  "confidence": res.get("confidence")})
        pool.remove(p)
        queued_addrs.add(k)
        log(f"drafted {created}/{allowed}: {lead} ({org}, research={res.get('confidence')})")
        # Flush state periodically so a crash costs at most a few rows of bookkeeping
        # rather than the whole run's record of who was mailed.
        if created % 10 == 0:
            jsave("pool.json", pool)
            jsave("sent_log.json", sent_log)

    # ---- persist ----
    for r in report.get("rescued", []):
        holds["replied"].append(r["email"])
    for b in ("replied", "bounced", "capped", "closed"):
        holds[b] = sorted(set(holds.get(b, [])))
    if not args.dry_run:
        jsave("pool.json", pool)
        jsave("sent_log.json", sent_log)
        jsave("holds.json", holds)
        meta.update({"last_run_date": today.isoformat(), "last_created": created,
                     "created_today": already + created,
                     "deficit_today": max(0, DAILY_CAP - (already + created)),
                     "pool_remaining": len(pool), "pending_at_start": pending,
                     "gh_live": True})
        jsave("meta.json", meta)
    report["skipped"] = dict(report["skipped"])
    if fatal_api:
        report["fatal_api_error"] = fatal_api
        # A run that lost the API but still delivered its work through the template path
        # is DEGRADED, not failed, and it must not shout the same way. Calling a run that
        # wrote hundreds of drafts a failure is how a red tick stops meaning anything, and
        # the operator then misses the run that genuinely broke. Still surfaced loudly
        # enough to get the balance fixed, because the template is a floor, not a plan.
        level = "warning" if created else "error"
        print(f"::{level}::Run stopped spending after {API_FAIL_STREAK} consecutive "
              f"account-level API failures. {created} drafts were created, "
              f"{template_used} of them by the zero-API template rather than the model. "
              f"Classification and reply-due detection continued on Gmail reads alone. "
              f"This is not transient: every later run degrades the same way until the "
              f"balance is restored. {fatal_api}", flush=True)
    # Real spend for this run, split by call site. research (web search) and craft
    # differ by roughly an order of magnitude and until now nothing recorded which.
    try:
        report["api_usage"] = llm.usage_summary()
        log("api usage: " + json.dumps(report["api_usage"]))
    except Exception as e:
        report["api_usage"] = {"error": str(e)[:120]}
    report["template_drafts"] = template_used
    report["summary"] = {"created": created, "template_drafts": template_used, "allowed": allowed, "pending_before": pending,
                         "recycled_into_pool": report.get("recycled_into_pool", 0),
                         "created_today_total": already + created,
                         "deficit_today": max(0, DAILY_CAP - (already + created)),
                         "stale_queued": report.get("stale_queued", 0),
                         "queue_max": QUEUE_MAX, "daily_cap": DAILY_CAP,
                         "rescued": len(report.get("rescued", [])),
                         "pool_remaining": len(pool), "scanned": scanned,
                         "examined": examined,
                         "reply_due": len(report["reply_due"]), "held": len(report["held"])}
    jsave(RPT, report)
    log(json.dumps(report["summary"]))
    # Exit 6 = the API died AND nothing replaced it, so the run delivered nothing. Distinct
    # from 0 so the workflow goes red, and from 2/4/5 so the cause reads off the code alone.
    #
    # If the template path carried the run, exit 0: the work happened. A red tick on a run
    # that wrote hundreds of drafts trains the operator to ignore red ticks, and then the
    # genuinely broken run goes unnoticed. The ::warning:: above still says the balance
    # needs fixing, and summary.template_drafts says how much of the day leaned on the
    # floor rather than the model.
    if fatal_api and not created:
        return 6
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
