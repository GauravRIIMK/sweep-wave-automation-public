# -*- coding: utf-8 -*-
"""END-TO-END test of the real orchestrator against a fake mailbox and a fake model.

Everything under test is the production code path: run_daily.main() with its real gates,
real classification, real crafting prompt assembly, real validation and real MIME building.
Only the two network edges are replaced, so this exercises what secrets would otherwise
hide: gate ordering, capacity maths, verdict routing, state persistence, crash recovery
and the double-mail defences.

Run: python -m tests.test_e2e
"""
import base64, datetime, json, os, shutil, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import run_daily, craft, research, llm, classify
from pipeline import registries as R

FAILS = []
DAY = 86400000
NOW = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  <-- {extra}"))
    if not cond:
        FAILS.append(name)


# --------------------------------------------------------------------------- fake mailbox
def m(sender, text, days_ago, subject="Job Application | Acme: Marketing / Growth", mid=None):
    return {"id": mid or f"m{abs(hash((sender, days_ago, text))) % 10**9}",
            "sender": sender, "to": "", "date_ms": NOW - days_ago * DAY, "subject": subject,
            "message_id_hdr": "<contact3@mail.gmail.com>", "text": text, "labels": []}


ME = "rathore.gaurav1007@gmail.com"


def outbound(name, days, extra=""):
    return m(ME, f"Hi {name},\n\nI wrote about growth and marketing roles.{extra}", days)


class FakeGmail:
    """Implements exactly the surface run_daily uses."""

    def __init__(self, threads, account=ME, drafts=None, fail_on=None, no_recent_sends=False):
        self.threads = threads              # {tid: [msg,...]}
        self.account = account
        self.drafts = drafts or []          # [{draft_id,msg_id,threadId,to,internalDate}]
        self.created = []                   # [(to, tid, body)]
        self.fail_on = fail_on or set()
        self.no_recent_sends = no_recent_sends
        self.raws = []

    def profile_email(self):
        return self.account

    def search_messages(self, q, cap=2000):
        if self.no_recent_sends and "newer_than" in q:
            return []
        want = None
        if q.startswith("to:"):
            want = q[3:].strip().lower()
        out = []
        for tid, msgs in self.threads.items():
            for x in msgs:
                if x["sender"] != ME:
                    continue
                if want and (x.get("to") or "").lower() != want:
                    continue
                out.append({"id": x["id"], "threadId": tid})
        return out

    def message_meta(self, mid, headers=None):
        for tid, msgs in self.threads.items():
            for x in msgs:
                if x["id"] == mid:
                    lead = next((t for t in [x.get("to")] if t), None) or self._lead(tid)
                    return {"id": mid, "threadId": tid, "internalDate": x["date_ms"],
                            "labels": [], "snippet": x["text"][:80],
                            "headers": {"to": lead or "", "from": x["sender"],
                                        "subject": x["subject"]}}
        return None

    def _lead(self, tid):
        return self.threads[tid][0].get("lead", "")

    def thread_full(self, tid):
        if tid in self.fail_on:
            raise RuntimeError("simulated thread fetch failure")
        msgs = self.threads.get(tid)
        if msgs is None:
            return None
        return [dict(x) for x in sorted(msgs, key=lambda z: z["date_ms"])]

    def list_drafts(self):
        return [{"draft_id": d["draft_id"], "msg_id": d["msg_id"], "threadId": d["threadId"]}
                for d in self.drafts]

    def draft_detail(self, did):
        for d in self.drafts:
            if d["draft_id"] == did:
                return d
        return None

    def create_reply_draft(self, to_addr, thread_id, anchor, body_text):
        from pipeline.gmail_api import Gmail
        raw = Gmail.create_reply_draft.__wrapped__ if False else None
        # build the MIME exactly as production does, then keep it for assertions
        subj = anchor.get("subject", "") or ""
        if not subj.lower().startswith("re:"):
            subj = "Re: " + subj
        lines = [f"To: {to_addr}", f"Subject: {subj}",
                 "Content-Type: text/plain; charset=UTF-8", "MIME-Version: 1.0"]
        mid_hdr = anchor.get("message_id_hdr", "")
        if mid_hdr:
            lines += [f"In-Reply-To: {mid_hdr}", f"References: {mid_hdr}"]
        mime = "\r\n".join(lines) + "\r\n\r\n" + body_text
        self.raws.append((to_addr, base64.urlsafe_b64encode(mime.encode("utf-8")).decode("ascii")))
        self.created.append((to_addr, thread_id, body_text))
        did = f"d{len(self.created)}"
        self.drafts.append({"draft_id": did, "msg_id": f"x{len(self.created)}",
                            "threadId": thread_id, "to": to_addr, "internalDate": NOW})
        return {"id": did}


# --------------------------------------------------------------------------- fake model
GOOD_TEMPLATE = """Hi {first},

I wrote in {month} about growth and marketing roles at {org}, and I remain genuinely interested.

- Growth at scale: Senior Manager, Growth Operations at Blinkit Bistro, owning P&L across 50 pods
- Revenue from zero: at upGrad, built a referral engine to Rs 1.5 Cr in four months
- Sharp on numbers: hands on with SQL and cohort economics, MBA from IIM Kozhikode

If a growth or strategy seat is open near you, could you point me to the right person?

Thank you for your time.

Warm regards,
Gaurav Rathore
{phone}
LinkedIn: {li}"""

STATE_BAD_FIRST = {"n": 0}


def fake_call(system, user, model=None, max_tokens=0, tools=None, retries=3):
    """Extract the recipient from the assembled prompt so we prove craft.py passes it."""
    first = user.split("Recipient: ", 1)[1].split(" at ", 1)[0].strip()
    org = user.split(" at ", 1)[1].split(" (", 1)[0].strip()
    month = user.split("Month he last wrote to them: ", 1)[1].splitlines()[0].strip()
    body = GOOD_TEMPLATE.format(first=first, month=month, org=org,
                                phone=R.PHONE, li=R.REQUIRED_SIGNATURE)
    if "dev.recover@acme.com" in user and "REJECTED" not in user:
        body = body.replace("Rs 1.5 Cr", "Rs 15 Cr")        # bad first attempt, fixed on retry
    if "jatin.hopeless@acme.com" in user:
        body = body.replace("Rs 1.5 Cr", "Rs 15 Cr")        # never fixes -> must be HELD
    return body


def fake_research(email, first, org, subject=""):
    if "lowconf" in email:
        return {"confidence": "low", "match_evidence": "ambiguous namesake", "title": None,
                "team": None, "org_context": None, "hook": None, "open_roles": []}
    return {"confidence": "high", "match_evidence": "linkedin match", "title": "Director, Growth",
            "team": "Consumer Growth", "org_context": "scaling the consumer funnel",
            "hook": "the team publishes funnel benchmarks", "open_roles": []}


# --------------------------------------------------------------------------- harness
def build_mailbox():
    t = {}

    def add(tid, lead, msgs):
        for x in msgs:
            x["lead"] = lead
            if not x.get("to"):        # an explicit to= (the forward case) is preserved
                x["to"] = lead if x["sender"] == ME else ME
            x.setdefault("to_name", "")
        t[tid] = msgs
        return tid

    add("t_eligible", "trisha.hegde@acme.com", [outbound("Trisha", 120), outbound("Trisha", 60)])
    add("t_lowconf", "yamini.lowconf@acme.com", [outbound("Yamini", 90)])
    add("t_reply", "eshan.easwaran@acme.com",
        [outbound("Eshan", 100), m("eshan.easwaran@acme.com", "Thanks, let me check internally.", 30)])
    add("t_bounce", "ekta.dead@acme.com",
        [outbound("Ekta", 100),
         m("mailer-daemon@googlemail.com", "Address not found. Your message wasn't delivered", 99)])
    add("t_promise", "bhavna.rested@acme.com",
        [outbound("Bhavna", 100), outbound("Bhavna", 60, " This is the last note I will send.")])
    add("t_cap", "manav.capped@acme.com", [outbound("Manav", 200 - i * 10) for i in range(6)])
    # touched inside the quiet window (MIN_QUIET_DAYS, currently 3): must not be mailed
    add("t_recent", "kriti.recent@acme.com", [outbound("Kriti", 1)])
    add("t_banned", "rhea.x@vedantu.com", [outbound("Rhea", 90)])
    add("t_personal", "gautami.x@gmail.com", [outbound("Gautami", 90)])
    add("t_noname", "noname@acme.com", [m(ME, "Hi there,\n\nnote", 90)])
    add("t_recover", "dev.recover@acme.com", [outbound("Dev", 90)])
    add("t_hopeless", "jatin.hopeless@acme.com", [outbound("Jatin", 90)])
    add("t_queued", "darsh.queued@acme.com", [outbound("Darsh", 90)])
    # one human, two threads: the promise lives in the OTHER thread
    add("t_split_a", "sahil.split@acme.com", [outbound("Sahil", 90)])
    add("t_split_b", "sahil.split@acme.com",
        [outbound("Sahil", 70, " Fifth and last note from me; I will not write to you again.")])
    # one human, two threads, 3 + 3 touches = 6 = the per-human cap
    add("t_cap_a", "bhuvan.splitcap@acme.com", [outbound("Bhuvan", 150 - i * 10) for i in range(3)])
    add("t_cap_b", "bhuvan.splitcap@acme.com", [outbound("Bhuvan", 110 - i * 10) for i in range(3)])
    # legitimate org whose domain contains an ATS substring
    add("t_unilever", "contact4@unilever.com", [outbound("Lavanya", 90)])
    # machine address the old pattern missed
    add("t_jobalert", "contact5@jobalerts-ta.pwc.com", [outbound("Alerts", 90)])
    # ONE human reachable at two spellings (dot variant) on two different threads
    add("t_alias_a", "varun.omkar@policybazaar.com", [outbound("Varun", 90)])
    add("t_alias_b", "nandinikamath@policybazaar.com", [outbound("Varun", 88)])
    # a human replying from a shared recruiting inbox must count as a reply
    add("t_shared", "trisha.k@acme.com",
        [outbound("Trisha", 100),
         m("careers@acme.com", "Hi Gaurav, Trisha passed this to us. Nothing at Manager level now.", 20)])
    # human prose that the old bounce regex misread as an undeliverable address
    add("t_prose", "ujjwal.prose@acme.com",
        [outbound("Ujjwal", 100),
         m("ujjwal.prose@acme.com", "Thanks Gaurav. Hiring is blocked until Q3, ping me then.", 30)])
    # the thread was later forwarded to someone else; the pool row is that other person
    add("t_fwd", "farah.ibrahim@acme.com",
        [dict(outbound("Trisha", 120), to="trisha.hegde@acme.com"),
         m(ME, "Hi Farah,\n\nforwarding my note below", 40)])
    for i in range(6):
        add(f"t_bulk{i}", f"ravi.bulk{i}@acme.com", [outbound("Ravi", 90)])
    return t


def run(tmp, mailbox, *, argv=("--limit", "0"), account=ME, drafts=None, pool=None,
        meta=None, holds=None, cap="180", queue_max="900",
        template_fallback=True, no_recent_sends=False):
    run_daily.STATE = tmp
    os.makedirs(tmp, exist_ok=True)
    leads = pool if pool is not None else [
        {"email": v[0]["lead"], "thread_id": k} for k, v in mailbox.items()]
    json.dump(leads, open(os.path.join(tmp, "pool.json"), "w", encoding="utf-8"))
    json.dump(meta or {}, open(os.path.join(tmp, "meta.json"), "w", encoding="utf-8"))
    json.dump(holds or {"replied": [], "bounced": [], "capped": [], "review": []},
              open(os.path.join(tmp, "holds.json"), "w", encoding="utf-8"))
    json.dump([], open(os.path.join(tmp, "sent_log.json"), "w", encoding="utf-8"))
    json.dump({"acme.com": "Acme"}, open(os.path.join(tmp, "org_names.json"), "w", encoding="utf-8"))
    json.dump([], open(os.path.join(tmp, "priority_orgs.json"), "w", encoding="utf-8"))

    g = FakeGmail(mailbox, account=account, drafts=list(drafts or []),
                  no_recent_sends=no_recent_sends)
    run_daily.Gmail = lambda: g
    run_daily.DAILY_CAP = int(cap)
    # Pin both, always. DAILY_CAP bounds what ONE RUN CREATES; QUEUE_MAX bounds how
    # deep the unsent queue may get (7b696da split them after a single number doing
    # both jobs created nothing for three days, then authorised a 181-draft burst).
    # TEMPLATE_FALLBACK decides whether an unfixable draft is held or templated.
    run_daily.QUEUE_MAX = int(queue_max)
    run_daily.TEMPLATE_FALLBACK = bool(template_fallback)
    os.environ.update({"GMAIL_CLIENT_ID": "x", "GMAIL_CLIENT_SECRET": "x",
                       "GMAIL_REFRESH_TOKEN": "x", "ANTHROPIC_API_KEY": "x"})
    sys.argv = ["run_daily"] + list(argv)
    rc = run_daily.main()
    st = {}
    for f in ("pool.json", "sent_log.json", "holds.json", "meta.json"):
        p = os.path.join(tmp, f)
        st[f] = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None
    return rc, g, st


def main():
    craft.llm.call = fake_call
    research.research_lead = fake_research
    run_daily.research.research_lead = fake_research

    print("\n== E2E 1: full run, every verdict class ==")
    tmp = tempfile.mkdtemp()
    rc, g, st = run(tmp, build_mailbox())
    mailed = sorted(a for a, _, _ in g.created)
    check("exit 0", rc == 0, rc)
    check("eligible lead mailed", "trisha.hegde@acme.com" in mailed, mailed)
    check("low-confidence research still mailed (thread-grounded)", "yamini.lowconf@acme.com" in mailed)
    check("replied lead NOT mailed", "eshan.easwaran@acme.com" not in mailed)
    check("bounced lead NOT mailed", "ekta.dead@acme.com" not in mailed)
    check("silence-promised NOT mailed", "bhavna.rested@acme.com" not in mailed)
    check("6-touch capped NOT mailed", "manav.capped@acme.com" not in mailed)
    check("recently-touched NOT mailed", "kriti.recent@acme.com" not in mailed)
    check("banned org NOT mailed", "rhea.x@vedantu.com" not in mailed)
    check("personal mailbox NOT mailed", "gautami.x@gmail.com" not in mailed)
    check("nameless thread NOT mailed", "noname@acme.com" not in mailed)
    check("bad draft fixed on retry IS mailed", "dev.recover@acme.com" in mailed)
    # DEFAULT behaviour since 2026-09-24 (e928a47): an unfixable model draft is not
    # held, it is replaced by deterministic template copy. The invariant that
    # survives is narrower and is the one that matters: the REJECTED body never
    # leaves. "Rs 15 Cr" is the 10x overstatement the fixture puts in the bad body.
    jatin_body = next((b for a, _, b in g.created if a == "jatin.hopeless@acme.com"), "")
    check("unfixable lead IS mailed, by the template floor",
          "jatin.hopeless@acme.com" in mailed, mailed)
    check("but the rejected body never leaves", "Rs 15 Cr" not in jatin_body,
          jatin_body[:80])

    # ...and the hold path still works when the floor is off, so retiring the old
    # assertion does not retire the coverage.
    tmp_nf = tempfile.mkdtemp()
    _, g_nf, st_nf = run(tmp_nf, build_mailbox(), template_fallback=False)
    nf_mailed = [a for a, _, _ in g_nf.created]
    check("fallback off: unfixable draft HELD not mailed",
          "jatin.hopeless@acme.com" not in nf_mailed, nf_mailed)
    check("fallback off: unfixable lead recorded for review",
          "jatin.hopeless@acme.com" in st_nf["holds.json"]["review"],
          list(st_nf["holds.json"]["review"]))
    check("replied lead moved to holds", "eshan.easwaran@acme.com" in st["holds.json"]["replied"])
    check("bounced lead moved to holds", "ekta.dead@acme.com" in st["holds.json"]["bounced"])
    check("sent_log matches drafts created", len(st["sent_log.json"]) == len(g.created))
    check("meta records the run date", st["meta.json"].get("last_run_date") ==
          datetime.date.today().isoformat())
    check("gh_live set for the local interlock", st["meta.json"].get("gh_live") is True)
    check("mailed leads removed from pool",
          not any(p["email"] in mailed for p in st["pool.json"]))
    check("no lead mailed twice", len(mailed) == len(set(mailed)), mailed)

    print("\n== E2E 1b: cross-thread and registry regressions ==")
    check("promise in ANOTHER thread still silences the lead",
          "sahil.split@acme.com" not in mailed, mailed)
    check("touches summed across threads hit the per-human cap",
          "bhuvan.splitcap@acme.com" not in mailed, mailed)
    check("unilever.com is NOT blocked by the 'lever.co' ATS entry",
          "contact4@unilever.com" in mailed, mailed)
    check("jobalerts machine address blocked",
          "contact5@jobalerts-ta.pwc.com" not in mailed, mailed)

    print("\n== E2E 1c: audit regressions ==")
    alias_hits = [a for a in mailed if a.endswith("@policybazaar.com")]
    check("two spellings of ONE human produce ONE mail", len(alias_hits) == 1, alias_hits)
    check("reply from a shared recruiting inbox counts as a reply",
          "trisha.k@acme.com" not in mailed, mailed)
    check("'hiring is blocked' is a reply, not a bounce",
          "ujjwal.prose@acme.com" not in mailed and "ujjwal.prose@acme.com" in st["holds.json"]["replied"],
          st["holds.json"]["bounced"])
    # Farah legitimately received a mail addressed to him, so he may be written to; the
    # requirement is that he is never greeted with the ORIGINAL recipient's name.
    fwd = [b for a, _, b in g.created if a == "farah.ibrahim@acme.com"]
    check("forwarded thread greets the actual recipient, never the original",
          not fwd or (fwd[0].startswith("Hi Farah,") and "Trisha" not in fwd[0]),
          fwd[0][:40] if fwd else "not mailed")
    check("answered leads are pruned from the pool, not left to starve the budget",
          not any(p["email"] == "eshan.easwaran@acme.com" for p in st["pool.json"]))

    print("\n== E2E 2: content and threading of a real crafted mail ==")
    to, tid, body = next(x for x in g.created if x[0] == "trisha.hegde@acme.com")
    check("reply lands in the lead's own thread", tid == "t_eligible", tid)
    check("greets the name used in that thread", body.startswith("Hi Trisha,"), body[:30])
    check("uses the org label, not the raw domain", "at Acme" in body)
    check("no em or en dash", "—" not in body and "–" not in body)
    check("Rs 1.5 Cr not Rs 15 Cr", "Rs 1.5 Cr" in body and "Rs 15 Cr" not in body)
    check("canonical LinkedIn slug", R.REQUIRED_SIGNATURE in body)
    enc = next(r for a, r in g.raws if a == "trisha.hegde@acme.com")
    raw = base64.urlsafe_b64decode(enc + "==").decode("utf-8")
    check("MIME carries In-Reply-To", "In-Reply-To: <contact3@mail.gmail.com>" in raw)
    check("MIME subject is a reply", "Subject: Re: Job Application" in raw)
    check("MIME subject not double-prefixed", "Re: Re:" not in raw)
    check("MIME body decodes back intact", body.splitlines()[0] in raw)

    print("\n== E2E 3: gates ==")
    tmp2 = tempfile.mkdtemp()
    rc2, g2, _ = run(tmp2, build_mailbox(), account="gaurav.rathore11077@gmail.com")
    check("wrong mailbox aborts", rc2 == 2 and not g2.created, rc2)

    tmp3 = tempfile.mkdtemp()
    rc3, g3, _ = run(tmp3, build_mailbox(),
                     meta={"last_run_date": datetime.date.today().isoformat()})
    check("second run same day creates nothing", not g3.created)
    check("second run same day exits 0 (benign)", rc3 == 0, rc3)

    tmp4 = tempfile.mkdtemp()
    rc4, g4, _ = run(tmp4, build_mailbox(), no_recent_sends=True)
    check("no sends in 2 days -> abort (real stall)", rc4 == 3 and not g4.created, rc4)

    # Long-abandoned drafts are NOT a stall. This mailbox carries drafts weeks old that
    # never send; treating them as evidence of breakage aborted every run forever.
    tmp4b = tempfile.mkdtemp()
    abandoned = [{"draft_id": f"s{i}", "msg_id": f"sm{i}", "threadId": f"z{i}",
                  "to": f"old{i}@x.com", "internalDate": NOW - 20 * DAY} for i in range(38)]
    rc4b, g4b, _ = run(tmp4b, build_mailbox(), drafts=abandoned, cap="60")
    check("38 abandoned drafts do NOT abort the run", rc4b == 0 and len(g4b.created) > 0,
          (rc4b, len(g4b.created)))
    check("abandoned drafts do not eat the daily budget", len(g4b.created) > 5, len(g4b.created))

    tmp5 = tempfile.mkdtemp()
    full = [{"draft_id": f"f{i}", "msg_id": f"fm{i}", "threadId": f"y{i}",
             "to": f"p{i}@x.com", "internalDate": NOW} for i in range(180)]
    rc5, g5, _ = run(tmp5, build_mailbox(), drafts=full, cap="180", queue_max="180")
    check("queue at QUEUE_MAX creates nothing", not g5.created, len(g5.created))

    tmp6 = tempfile.mkdtemp()
    partial = [{"draft_id": f"h{i}", "msg_id": f"hm{i}", "threadId": f"w{i}",
                "to": f"q{i}@x.com", "internalDate": NOW} for i in range(177)]
    rc6, g6, _ = run(tmp6, build_mailbox(), drafts=partial, cap="180", queue_max="180")
    check("queue headroom gate allows exactly 180-177=3", len(g6.created) == 3,
          len(g6.created))

    print("\n== E2E 4: double-mail defences ==")
    tmp7 = tempfile.mkdtemp()
    queued = [{"draft_id": "q1", "msg_id": "qm1", "threadId": "t_queued",
               "to": "darsh.queued@acme.com", "internalDate": NOW}]
    rc7, g7, _ = run(tmp7, build_mailbox(), drafts=queued)
    check("lead with a queued draft is skipped (state-loss defence)",
          "darsh.queued@acme.com" not in [a for a, _, _ in g7.created])

    tmp8 = tempfile.mkdtemp()
    mb = build_mailbox()
    dup_pool = [{"email": "trisha.hegde@acme.com", "thread_id": "t_eligible"},
                {"email": "Trisha.Hegde@acme.com", "thread_id": "t_eligible"}]
    rc8, g8, _ = run(tmp8, mb, pool=dup_pool)
    sent8 = [a for a, _, _ in g8.created]
    check("duplicate pool rows collapse to one mail",
          sent8.count("trisha.hegde@acme.com") == 1, sent8)

    tmp8b = tempfile.mkdtemp()
    mb8b = build_mailbox()
    rc8b, g8b, st8b = run(tmp8b, mb8b)               # day 1
    day1 = sorted(a for a, _, _ in g8b.created)
    # day 2: same mailbox, state carried forward, drafts already dispatched (queue empty)
    run_daily.STATE = tmp8b
    json.dump({}, open(os.path.join(tmp8b, "meta.json"), "w", encoding="utf-8"))
    g8c = FakeGmail(mb8b, drafts=[])
    run_daily.Gmail = lambda: g8c
    sys.argv = ["run_daily"]
    run_daily.main()
    day2 = sorted(a for a, _, _ in g8c.created)
    check("already-mailed leads are not rediscovered the next day",
          not set(day1) & set(day2), sorted(set(day1) & set(day2)))

    tmp9 = tempfile.mkdtemp()
    mb9 = build_mailbox()
    mb9["t_sentnostate"] = [dict(x, lead="lost.banerjee@acme.com", to="lost.banerjee@acme.com")
                            for x in [outbound("Lost", 0)]]
    rc9, g9, _ = run(tmp9, mb9,
                     pool=[{"email": "lost.banerjee@acme.com", "thread_id": "t_sentnostate"}])
    check("state lost after dispatch: thread proves it, so no re-mail",
          "lost.banerjee@acme.com" not in [a for a, _, _ in g9.created])

    print("\n== E2E 5: resilience ==")
    tmp10 = tempfile.mkdtemp()
    mb10 = build_mailbox()
    run_daily.STATE = tmp10
    g10 = FakeGmail(mb10, fail_on={"t_eligible"})
    os.makedirs(tmp10, exist_ok=True)
    for f, v in (("pool.json", [{"email": v2[0]["lead"], "thread_id": k}
                                for k, v2 in mb10.items()]), ("meta.json", {}),
                 ("holds.json", {"replied": [], "bounced": [], "capped": [], "review": []}),
                 ("sent_log.json", []), ("org_names.json", {"acme.com": "Acme"}),
                 ("priority_orgs.json", [])):
        json.dump(v, open(os.path.join(tmp10, f), "w", encoding="utf-8"))
    run_daily.Gmail = lambda: g10
    sys.argv = ["run_daily"]
    rc10 = run_daily.main()
    check("one broken thread does not stop the run", rc10 == 0 and len(g10.created) > 0, rc10)
    rep = json.load(open(os.path.join(tmp10, "runs",
                                      datetime.date.today().isoformat() + ".json"), encoding="utf-8"))
    check("run report written to state/runs/", rep["summary"]["created"] == len(g10.created))
    check("failure recorded in the report", any("thread fetch" in e for e in rep["errors"]))

    tmp11 = tempfile.mkdtemp()
    rc11, g11, st11 = run(tmp11, build_mailbox(), argv=("--dry-run",))
    check("dry-run creates no drafts", not g11.created)
    check("dry-run leaves pool untouched", len(st11["pool.json"]) == len(build_mailbox()))

    tmp12 = tempfile.mkdtemp()
    rc12, g12, _ = run(tmp12, build_mailbox(), argv=("--limit", "2"))
    check("--limit caps the run", len(g12.created) == 2, len(g12.created))

    # ---- E2E 6: the exit-6 gate, which had NO coverage until 2026-09-28 -------------
    # Exit 6 means "the API died AND nothing replaced it, so the run delivered nothing".
    # The gate read `if fatal_api and not template_used`, which is a DIFFERENT question:
    # template_used counts template CRAFTS, created counts drafts that reached Gmail.
    # Where they agree the two spellings are indistinguishable, so both scenarios below
    # are built on the cases where they DIVERGE. An earlier version of this block tested
    # the agreeing cases, passed with the bug reintroduced, and was therefore worthless.
    FATAL = ('anthropic 400: {"type":"error","error":{"type":"invalid_request_error",'
             '"message":"Your credit balance is too low to access the Anthropic API."}}')
    real_call = craft.llm.call
    real_create = FakeGmail.create_reply_draft

    # (a) TOO RED. The model dies partway; the leads crafted before it died were really
    #     delivered. created > 0, template_used == 0. The old gate exited 6 on a run that
    #     did its work, which is how a red tick stops meaning anything. Six production
    #     runs in 30 days went red this way having delivered 23 to 208 drafts each.
    calls = {"n": 0}

    def dies_after_three(*a, **k):
        calls["n"] += 1
        if calls["n"] > 3:                    # > API_FAIL_STREAK consecutive failures
            raise RuntimeError(FATAL)
        return real_call(*a, **k)

    try:
        craft.llm.call = dies_after_three
        tmp13 = tempfile.mkdtemp()
        rc13, g13, _ = run(tmp13, build_mailbox(), template_fallback=False)
        check("API died partway but drafts were delivered -> exit 0, not 6",
              rc13 == 0 and len(g13.created) > 0, (rc13, len(g13.created)))
    finally:
        craft.llm.call = real_call

    # (b) TOO GREEN, and the more dangerous direction. The template floor crafts copy for
    #     every lead, then every single create_reply_draft throws, so nothing reaches the
    #     mailbox. template_used > 0, created == 0. The old gate exited 0 on a run that
    #     delivered literally nothing. These two counters really do diverge in production:
    #     state/runs/2026-09-24-run-2.json records created 490 against template_drafts 492.
    def always_fails(self, *a, **k):
        raise RuntimeError("gmail 500: draft creation failed")

    try:
        craft.llm.call = lambda *a, **k: (_ for _ in ()).throw(RuntimeError(FATAL))
        FakeGmail.create_reply_draft = always_fails
        tmp14 = tempfile.mkdtemp()
        rc14, g14, st14 = run(tmp14, build_mailbox(), template_fallback=True)
        check("template crafted but nothing reached Gmail -> exit 6, not 0",
              rc14 == 6 and not g14.created, (rc14, len(g14.created)))
    finally:
        craft.llm.call = real_call
        FakeGmail.create_reply_draft = real_create

    shutil.rmtree(tmp13, ignore_errors=True)
    shutil.rmtree(tmp14, ignore_errors=True)

    for d in (tmp, tmp2, tmp3, tmp4, tmp4b, tmp5, tmp6, tmp7, tmp8, tmp9, tmp10, tmp11, tmp12):
        shutil.rmtree(d, ignore_errors=True)

    print(f"\n{'ALL E2E TESTS PASSED' if not FAILS else str(len(FAILS)) + ' FAILURES: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
