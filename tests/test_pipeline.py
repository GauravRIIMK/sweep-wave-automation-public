# -*- coding: utf-8 -*-
"""Offline correctness tests. No secrets, no network: they prove the gates that keep
bad mail from being created. Run with: python -m tests.test_pipeline"""
import datetime, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import classify, validate
from pipeline import registries as R

FAILS = []
NOW = int(datetime.datetime(2026, 9, 10, tzinfo=datetime.timezone.utc).timestamp() * 1000)
DAY = 86400000


def check(name, cond, extra=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {extra}")
        FAILS.append(name)


LEAD = "trisha.hegde@acme.com"


def msg(sender, text, days_ago, subject="Job Application | Acme: Marketing / Growth", to=None):
    return {"id": f"m{days_ago}", "sender": sender,
            "to": to if to is not None else (LEAD if sender == ME else ME),
            "to_name": "Trisha Hegde", "date_ms": NOW - days_ago * DAY,
            "subject": subject, "message_id_hdr": "<x@mail>", "text": text, "labels": [],
            "threadId": "t1", "date_iso": "2026-07-01"}


ME = "rathore.gaurav1007@gmail.com"
GOOD_THREAD = [
    msg(ME, "Hi Trisha,\n\nScaling acquisition funnels without cratering unit economics.", 90),
    msg(ME, "Hi Trisha,\n\nI wrote in June about growth roles at Acme.", 40),
]

print("\n== classify ==")
v = classify.classify("trisha.hegde@acme.com", GOOD_THREAD, NOW)
check("eligible thread is ELIGIBLE", v["verdict"] == "ELIGIBLE", v)
check("first name extracted", v["first"] == "Trisha", v.get("first"))

v = classify.classify("trisha.hegde@acme.com", GOOD_THREAD + [
    msg("trisha.hegde@acme.com", "Thanks for writing, let me check internally.", 5)], NOW)
check("human reply -> REPLY_DUE", v["verdict"] == "REPLY_DUE", v)

v = classify.classify("trisha.hegde@acme.com", GOOD_THREAD + [
    msg("mailer-daemon@googlemail.com", "Address not found. Your message wasn't delivered", 5)], NOW)
check("bounce -> SKIP", v["verdict"] == "SKIP" and "bounce" in v["reason"], v)

v = classify.classify("trisha.hegde@acme.com", GOOD_THREAD + [
    msg(ME, "Hi Trisha,\n\nThis is the last note I will send about this.", 30)], NOW)
check("silence promise -> SKIP", v["verdict"] == "SKIP" and "silence" in v["reason"], v)

v = classify.classify("trisha.hegde@acme.com",
                      [msg(ME, "Hi Trisha,\n\nnote", 100 - i) for i in range(6)], NOW)
check("6 touches -> SKIP (cap)", v["verdict"] == "SKIP" and "cap" in v["reason"], v)

v = classify.classify("trisha.hegde@acme.com",
                      [msg(ME, "Hi Trisha,\n\nnote", 3)], NOW)
check("recent touch -> SKIP", v["verdict"] == "SKIP" and "ago" in v["reason"], v)

v = classify.classify("someone@vedantu.com", GOOD_THREAD, NOW)
check("banned org -> SKIP", v["verdict"] == "SKIP" and "blocked" in v["reason"], v)
v = classify.classify("someone@ril.com", GOOD_THREAD, NOW)
check("Reliance blocked", v["verdict"] == "SKIP", v)

# THE BANNED-ORG FILTER MUST NOT GAG INBOUND.
# The ban is a rule about not sending COLD OUTREACH. Applying it to a human who has
# WRITTEN TO US once buried a Vedantu hiring contact asking for a resume for five weeks,
# plus a contact writing from a hospital bed. It regressed on 2026-09-10 when "deloitte"
# was added to BLOCK_ORG_TOKENS: correct for outbound, but it would have gagged replies
# from the 21 Deloitte leads still in the pool. The block check now runs only when nobody
# has written to us.
_BANNED = "contact6@deloitte.com"
_BANNED_REPLIED = [msg(ME, "Hi Trisha,\n\nnote", 72, to=_BANNED),
                   msg(_BANNED, "Can you share your resume?", 3, to=ME)]
v = classify.classify("contact6@deloitte.com", _BANNED_REPLIED, NOW)
check("a reply from a BLOCKED org is still surfaced as REPLY_DUE",
      v["verdict"] == "REPLY_DUE", v)
check("...and it carries a note that the org is blocked",
      "blocked" in v["reason"], v)

# ...while the same org with NO reply is still blocked for outbound
v = classify.classify("contact6@deloitte.com", [msg(ME, "Hi Trisha,\n\nnote", 72)], NOW)
check("the same blocked org with no reply is still SKIP/blocked",
      v["verdict"] == "SKIP" and v["state"] == "blocked", v)
v = classify.classify("contact7@gmail.com", GOOD_THREAD, NOW)
check("personal mailbox blocked", v["verdict"] == "SKIP", v)
v = classify.classify("careers@acme.com", GOOD_THREAD, NOW)
check("machine address blocked", v["verdict"] == "SKIP", v)
v = classify.classify("charu@swiggy.in", GOOD_THREAD, NOW)
check("do-not-mail address blocked", v["verdict"] == "SKIP", v)
v = classify.classify("x@salesforce.com", GOOD_THREAD, NOW)
check("Account B domain held", v["verdict"] == "SKIP" and "Account B" in v["reason"], v)

v = classify.classify("trisha.hegde@acme.com",
                      [msg(ME, "Hi there,\n\nnote", 60)], NOW)
check("no real name -> SKIP", v["verdict"] == "SKIP" and "name" in v["reason"], v)

print("\n== validate ==")
GOOD = f"""Hi Trisha,

I wrote in June about growth and marketing roles at Acme, and I remain genuinely interested.

- Growth at scale: Senior Manager, Growth Operations at Blinkit Bistro, owning P&L across 50 pods
- Revenue from zero: at upGrad, built a referral engine to Rs 1.5 Cr in four months
- Sharp on numbers: hands on with SQL and cohort economics, MBA from IIM Kozhikode

If a growth or strategy seat is open near you, could you point me to the right person?

Thank you for your time.

Warm regards,
Gaurav Rathore
{R.PHONE}
LinkedIn: {R.REQUIRED_SIGNATURE}"""

ok, why = validate.validate(GOOD, "Trisha", "trisha.hegde@acme.com")
check("clean mail passes", ok, why)

ok, _ = validate.validate(GOOD.replace("Hi Trisha,", "Dear Trisha,"), "Trisha", "trisha.hegde@acme.com")
check("'Dear' rejected", not ok)
ok, _ = validate.validate(GOOD.replace("in four months", "in four months — fast"), "Trisha", "trisha.hegde@acme.com")
check("em dash rejected", not ok)
ok, _ = validate.validate(GOOD.replace("I wrote in June", "Following up on my note in June"), "Trisha", "trisha.hegde@acme.com")
check("'following up' rejected", not ok)
ok, _ = validate.validate(GOOD.replace("Rs 1.5 Cr", "Rs 15 Cr"), "Trisha", "trisha.hegde@acme.com")
check("Rs 15 Cr rejected", not ok)
ok, _ = validate.validate(GOOD.replace(R.REQUIRED_SIGNATURE, "https://linkedin.com/in/gauravrathore"),
                          "Trisha", "trisha.hegde@acme.com")
check("wrong LinkedIn slug rejected", not ok)
ok, _ = validate.validate(GOOD.replace("Hi Trisha,", "Hi Rhea,"), "Trisha", "trisha.hegde@acme.com")
check("greeting/name mismatch rejected", not ok)
ok, _ = validate.validate(GOOD.replace("could you point me to the right person?",
                                       "could you point me? do you have 15 minutes?"), "Trisha", "trisha.hegde@acme.com")
check("two asks rejected", not ok)
ok, _ = validate.validate(GOOD.replace("50 pods", "412 pods"), "Trisha", "trisha.hegde@acme.com")
check("ungrounded number rejected", not ok)
ok, _ = validate.validate(GOOD.replace("I wrote in June about growth and marketing roles at Acme, and I remain genuinely interested.",
                                       "I hope this finds you well."), "Trisha", "trisha.hegde@acme.com")
check("flattery/filler rejected", not ok)

grounded = GOOD.replace("50 pods", "412 pods")
ok, _ = validate.validate(grounded, "Trisha", "trisha.hegde@acme.com",
                          research={"org_context": "team of 412 people"})
check("number grounded by research passes", ok)

print("\n== registries and identity ==")
for prose in ["Thanks Gaurav. Hiring is blocked until Q3, ping me then.",
              "That team does not exist anymore after the reorg.",
              "Sorry, my mailbox is full of these; let me forward you internally."]:
    v = classify.classify(LEAD, GOOD_THREAD + [msg(LEAD, prose, 20)], NOW)
    check(f"human prose is a reply, not a bounce: {prose[:34]!r}", v["verdict"] == "REPLY_DUE", v["reason"])

v = classify.classify(LEAD, GOOD_THREAD + [
    msg("careers@acme.com", "Hi Gaurav, Trisha passed this to us. Nothing open at Manager level.", 20)], NOW)
check("reply from a shared recruiting inbox counts as human", v["verdict"] == "REPLY_DUE", v["reason"])

v = classify.classify(LEAD, GOOD_THREAD + [
    msg("mailer-daemon@googlemail.com", "Mail delivery failed: returning message to sender", 5)], NOW)
check("Exim bounce wording detected", v["state"] == "bounced", v["reason"])

check("canon collapses dots", classify.canon("varun.omkar@pb.com") == classify.canon("nandinikamath@pb.com"))
check("canon collapses plus tags", classify.canon("a.b+jobs@x.com") == "contact8@x.com")
check("canon keeps different humans apart",
      classify.canon("eshan.easwaran@x.com") != classify.canon("hemant.easwaran@x.com"))
check("unilever not blocked", classify.blocked_reason("contact9@unilever.com") is None)
check("lever.co ATS still blocked", classify.blocked_reason("x@jobs.lever.co") is not None)

ok, _ = validate.validate(GOOD.replace("owning P&L across 50 pods",
                                       "lifting activation 68 percent"), "Trisha", LEAD)
check("invented percentage rejected (phone digits no longer ground it)", not ok)

print("")
print("== ungrounded familiarity ==")
LOW = {"confidence": "low"}
HIGH = {"confidence": "high", "org_context": "the consumer funnel"}
ORIG = "I wrote in June about growth and marketing roles at Acme, and I remain genuinely interested."
for line, label in [
        ("Given the IIM Kozhikode connection I am writing.", "shared connection"),
        ("As a fellow IIM alum I wanted to reach out.", "shared alma mater"),
        ("I saw your recent launch and wanted to reach out.", "seen their work"),
        ("Given your work on retention I wanted to reach out.", "asserts a fact")]:
    ok, _ = validate.validate(GOOD.replace(ORIG, line), "Trisha", LEAD, research=LOW)
    check("unverified person: reject " + label, not ok)
ok, _ = validate.validate(GOOD.replace(ORIG, "Given your work on the consumer funnel I am writing."),
                          "Trisha", LEAD, research=HIGH)
check("verified person: may reference researched work", ok)

print("")
print("== banned internal figures (2026-10-13) ==")
# 127 of 712 queued drafts carried a former employer's internal numbers. Two causes, both
# by construction: template_craft POINTERS[8] WAS the cannibalization line with the figure
# (2 of 10 pointers per render, so 20%; observed 17.8%), and CANDIDATE_FACTS handed the LLM
# crafter both figures as approved facts. No guard existed anywhere. Blinkit competes
# directly with Swiggy Instamart, Zepto, Flipkart Minutes and JioMart, all live targets.
_SIG = "\n\nWarm regards,\nGaurav Rathore\n" + R.PHONE + "\nLinkedIn: " + R.REQUIRED_SIGNATURE


def _banned(frag):
    body = ("Hi Trisha,\n\n" + frag + " I led go to market for 38 dark store launches at "
            "Blinkit and built the hyperlocal site selection framework. Managed station "
            "quality across about 50 cloud kitchens in 4 cities, and SQL and Python are "
            "hands-on for the analysis behind those calls." + _SIG)
    ok, reasons = validate.validate(body, "Trisha", "trisha@swiggy.in", {}, "")
    return any("banned internal figure" in r for r in reasons)


for _frag, _want in [
        ("A cannibalization study showed 60 to 70% order displacement.", True),
        ("The study showed 60-70% displacement.", True),
        ("The study showed sixty to seventy percent displacement.", True),
        ("Thirteen margin interventions projected to save about Rs 5.7L a month.", True),
        ("Across 121,582 orders I managed station quality.", True),
        # the sanctioned way to tell the same story: discipline kept, number gone
        ("I ran the cannibalization work that stopped a launch, because most of the orders "
         "it counted on came from a store we already had.", False)]:
    check(("banned figure caught: " if _want else "clean story allowed: ") + _frag[:40],
          _banned(_frag) == _want)

# the deterministic crafter must not be able to emit one at all
import itertools as _it
from pipeline import template_craft as _TC
_leak = 0
for _o, _p1, _p2, _a, _c in _it.product(_TC.OPENERS, _TC.POINTERS, _TC.POINTERS,
                                        _TC.ASKS, _TC.CLOSERS):
    if _p1 == _p2:
        continue
    _b = "\n".join([_o, _p1, _p2, _a, _c]).lower()
    if any(f in _b for f, _ in R.BANNED_FIGURES):
        _leak += 1
check("template corpus leaks no internal figure (8,640 renders)", _leak == 0, _leak)

print("== promise gate, mined from the real sent corpus ==")
import re as _re
_P = _re.compile(R.PROMISE_PAT, _re.I)
for _t, _e in [
        ("a one line no is genuinely helpful and I will stop here.", True),
        ("so this third note is the final", True),
        ("this fourth note is the last, so please", True),
        ("so this is my last to affinity", True),
        ("this third one is my last, and a one-line no is", True),
        ("this is where I stop", True),
        ("a brief and final follow-up", True),
        ("and I will leave it there", True),
        ("four notes is plenty, so i will stop after this one", True),
        ("I remain genuinely interested in the right fit", False),
        ("I am keen on the last mile delivery space", False),
        ("razorpay cli launch last month signals the shift", False),
        # --- 2026-10-13: the four that ESCAPED and were queued on 10-01 -------------
        # Each is one word away from a form the pattern already had, which is how a
        # list of surface forms fails. A promise breach is worse than a tone breach:
        # the recipient can see the quoted promise on the same screen.
        ("and it will be the last.", True),                        # Kyndryl
        ("I shall make this the closing one.", True),              # Maersk
        ("I will make this third one the last", True),             # MMC
        ("once more and then leave the thread alone.", True),      # Edelweiss
        # near-neighbours of the above, to stop the fix being over-fitted to four cases
        ("this would be my last note on it", True),
        ("I shall not write to you again about this", True),
        ("after this note I will leave it be", True),
        # and copy that must stay sendable, including the live reply copy of 2026-10-13
        ("I will take you up on the timing and write again around March", False),
        ("Thank you for saying you would come back before going external", False),
        ("I will call you on the number you sent", False),
        ("Could you point me to whoever is screening for this one?", False)]:
    check(("promise caught: " if _e else "not a promise: ") + _t[:44],
          bool(_P.search(_t)) == _e)

# =====================================================================================
# resend_failed: its OWN suppression gate
# =====================================================================================
# WHY THIS SECTION EXISTS
#
# resend_failed.py rebuilds a draft for every person a run reported it drafted whose mail
# never reached Sent. Until 2026-11-05 it had no suppression check of its own: the scope
# was filtered by DOMAIN only, then handed straight to template_craft.craft_template and
# g.create_reply_draft. A draft in this mailbox leaves within hours.
#
# It was not actually sending to held people, because validate() passes the recipient to
# the tone gate (ctx={"to": lead_email}) and S09_SUPPRESSED fires on that. But that single
# keyword argument, in another module, was the ONLY thing standing between the repair path
# and a bounced, capped, do-not-mailed or promised-silence address.
#
# The gate now reads five sources. These checks pin all five, and pin the ORDER, because
# a gate that runs after the craft is not a gate.
import json as _json
import tempfile as _tempfile
import inspect as _inspect

# resend_failed -> gmail_api -> `import requests`, which is not installed here: it is what
# tests/test_dispatch.py and tests/test_e2e.py die on. Nothing below makes a network call,
# so a placeholder module satisfies the import and keeps this gate under test in a suite
# that actually runs. Registered only when the real package is genuinely absent.
try:
    import requests as _real_requests      # noqa: F401
except ImportError:
    import types as _types
    sys.modules["requests"] = _types.ModuleType("requests")
from pipeline import resend_failed as RF

print("\n== resend_failed suppression gate ==")

_FIXDIR = _tempfile.mkdtemp(prefix="resend-gate-fixtures-")
_FIXN = [0]


def _fix(obj, as_text=None):
    """Write a fixture JSON file and return its path.

    Parsed as JSON by the loader, never grepped. Lives in one scratch directory that this
    section removes on the way out, so the suite leaves nothing behind.
    """
    _FIXN[0] += 1
    p = os.path.join(_FIXDIR, f"fixture-{_FIXN[0]}.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write(as_text if as_text is not None else _json.dumps(obj))
    return p


def _raises(fn, exc=RuntimeError):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


# -------------------------------------------------------------------------------------
# 1. holds.json: which buckets bar, and the two that deliberately do not
# -------------------------------------------------------------------------------------
check("suppressing buckets are exactly bounced/capped/closed",
      tuple(RF.SUPPRESSING_BUCKETS) == ("bounced", "capped", "closed"),
      RF.SUPPRESSING_BUCKETS)
check("replied is NOT a suppressing bucket (an answer is owed an answer)",
      "replied" not in RF.SUPPRESSING_BUCKETS)
check("review is NOT a suppressing bucket (sediment, not a decision)",
      "review" not in RF.SUPPRESSING_BUCKETS)
check("replied and review are both reported as notes",
      set(RF.NOTED_BUCKETS) == {"replied", "review"}, RF.NOTED_BUCKETS)

HP = _fix({
    "bounced": ["contact10@swiggy.in"],
    "capped":  ["contact11@flipkart.com"],
    "closed":  ["contact12@Zomato.Com"],
    "replied": ["contact13@swiggy.in"],
    "review":  {"contact14@swiggy.in": {"reasons": ["ungrounded number '2'"], "count": 1}},
})
barred, noted = RF.load_holds(HP)

check("bounced address is barred", barred.get("contact15@swiggy.in") == "bounced", barred)
check("capped address is barred", barred.get("contact11@flipkart.com") == "capped", barred)
check("closed address is barred (case folded)",
      barred.get("contact16@zomato.com") == "closed", barred)
check("replied address is NOT barred", "contact13@swiggy.in" not in barred, barred)
check("review address is NOT barred", "contact14@swiggy.in" not in barred, barred)
check("replied address IS noted", noted.get("contact13@swiggy.in") == "replied", noted)
check("review address IS noted", noted.get("contact14@swiggy.in") == "review", noted)

# holds.json fails LOUD. An unreadable gate must stop the run, not open it.
check("missing holds.json raises", _raises(lambda: RF.load_holds(
    os.path.join(_FIXDIR, "no-such-holds-file.json"))))
check("unparseable holds.json raises",
      _raises(lambda: RF.load_holds(_fix(None, as_text="{not json"))))
check("holds.json that is a list raises", _raises(lambda: RF.load_holds(_fix(["a@b.com"]))))
check("empty suppressing buckets raise rather than returning an empty set",
      _raises(lambda: RF.load_holds(_fix(
          {"bounced": [], "capped": [], "closed": [], "review": {"x@y.com": {}}}))))
check("a holds bucket of the wrong type raises",
      _raises(lambda: RF.load_holds(_fix({"bounced": "contact17@x.com", "capped": [],
                                          "closed": []}))))
check("review as a list (the pre-migration shape) is still read as notes",
      RF.load_holds(_fix({"bounced": ["a@b.com"], "capped": [], "closed": [],
                          "review": ["contact18@shape.com"]}))[1].get("contact18@shape.com") == "review")
check("non-string junk in a holds bucket is ignored, not crashed on",
      RF.load_holds(_fix({"bounced": ["a@b.com", None, 7, {"x": 1}], "capped": [],
                          "closed": []}))[0].get("a@b.com") == "bounced")
check("a bare domain in a holds bucket bars nothing",
      "swiggy.in" not in RF.load_holds(_fix(
          {"bounced": ["a@b.com", "swiggy.in", "@swiggy.in"], "capped": [],
           "closed": []}))[0])

# -------------------------------------------------------------------------------------
# 2. state/suppression.json: do-not-mail, promise-of-silence, org and gap domains
# -------------------------------------------------------------------------------------
# These sources are NOT in the repo -- the memory registry lives outside it, the promise
# ledgers are untracked -- so tools/build_suppression.py compiles them into one tracked
# file. In GitHub Actions that file IS the registry.
SP = _fix({
    "do_not_mail": {"stop.request@acme.com": "do-not-mail registry [A]: STOP_REQUEST"},
    "promised_silence": {"told.them.i.would.stop@acme.com": "promise ledger 2026-08-11: "
                                                            "so this will be the last one",
                         "stop.request@acme.com": "promise ledger: also promised"},
    # closedco.test deliberately, NOT a real banned org: every one of the five live
    # org-scope domains (airtel, airtelbank, blitzscale, shopdeck, noon) is ALSO caught
    # by registries.py, so a real one makes this branch and that one agree and neither
    # test can see the other go missing.
    "do_not_mail_orgs": {"closedco.test": "NO_OPENINGS_ORG org scope"},
    "registry_gap_domains": {"idfcfirstbank.com": "NO MAIL org-wide"},
})
_AB, _DB = RF.load_suppression(SP)

check("a do-not-mail address is barred",
      "stoprequest@acme.com" in _AB, sorted(_AB))
check("a promised-silence address is barred",
      "toldthemiwouldstop@acme.com" in _AB, sorted(_AB))
check("the PROMISE reason wins when an address is in both sets (worst reason reported)",
      _AB.get("stoprequest@acme.com", "").startswith("PROMISE OF SILENCE"),
      _AB.get("stoprequest@acme.com"))
check("a promise reason is labelled as one",
      "PROMISE OF SILENCE" in _AB.get("toldthemiwouldstop@acme.com", ""))
check("an org-scope domain is barred", _DB.get("closedco.test"), _DB)
check("a registry-gap domain is barred", _DB.get("idfcfirstbank.com"), _DB)
check("suppression addresses are canonicalised",
      all("." not in a.split("@")[0] for a in _AB), sorted(_AB))

# suppression.json fails LOUD too.
check("missing suppression.json raises", _raises(lambda: RF.load_suppression(
    os.path.join(_FIXDIR, "no-such-suppression.json"))))
check("unparseable suppression.json raises",
      _raises(lambda: RF.load_suppression(_fix(None, as_text="{nope"))))
check("suppression.json that is a list raises",
      _raises(lambda: RF.load_suppression(_fix([1, 2]))))
check("suppression.json with no addresses at all raises",
      _raises(lambda: RF.load_suppression(_fix(
          {"do_not_mail": {}, "promised_silence": {},
           "do_not_mail_orgs": {"x.com": "y"}}))))
check("a wrongly typed suppression section raises",
      _raises(lambda: RF.load_suppression(_fix({"do_not_mail": ["a@b.com"]}))))
check("a wrongly typed domain section raises",
      _raises(lambda: RF.load_suppression(_fix({"do_not_mail": {"a@b.com": "r"},
                                                "do_not_mail_orgs": ["x.com"]}))))

# -------------------------------------------------------------------------------------
# 3. domain_bar: EXACT or SUBDOMAIN SUFFIX, never a substring
# -------------------------------------------------------------------------------------
# The incident this encodes: a substring ban on "lever.co" blocked every address at
# unilever.com and removed 21 legitimate leads at a target company. Synthetic domains are
# used so the case under test is the matching rule itself, not the shared registry.
_DOMS = {"holdco.test": "org scope", "hire.test": "org scope"}
check("exact domain is barred", RF.domain_bar("a@holdco.test", _DOMS))
check("subdomain of a barred domain is barred", RF.domain_bar("a@mail.holdco.test", _DOMS))
check("a domain that merely ENDS WITH the letters is NOT barred",
      RF.domain_bar("a@unholdco.test", _DOMS) is None)
check("shire-style near miss is NOT barred", RF.domain_bar("a@shire.test", _DOMS) is None)
check("a longer TLD-ish sibling is NOT barred",
      RF.domain_bar("a@holdco.test.in", _DOMS) is None)
check("an unrelated domain is NOT barred", RF.domain_bar("a@swiggy.in", _DOMS) is None)
check("an address with no domain bars nothing", RF.domain_bar("", _DOMS) is None)

# And the real registry still gets the real pair right.
check("the shared registry blocks lever.co",
      RF.suppressed_reason("a@lever.co", {}, {}) is not None)
check("the shared registry does NOT block unilever.com",
      RF.suppressed_reason("a@unilever.com", {}, {}) is None)

# -------------------------------------------------------------------------------------
# 4. suppressed_reason: the whole decision, and it refuses to be called blind
# -------------------------------------------------------------------------------------
check("suppressed_reason refuses a None address set (dispatch's own contract)",
      _raises(lambda: RF.suppressed_reason("a@b.com", None, {}), ValueError))
check("suppressed_reason refuses a None domain set",
      _raises(lambda: RF.suppressed_reason("a@b.com", {}, None), ValueError))
check("a clean lead has no reason", RF.suppressed_reason("fresh@swiggy.in", _AB, _DB) is None)
check("a do-not-mail lead has a reason",
      RF.suppressed_reason("stop.request@acme.com", _AB, _DB) is not None)
check("an org-scope lead is barred by domain",
      RF.suppressed_reason("contact19@closedco.test", _AB, _DB) is not None)
check("a subdomain of an org-scope domain is barred",
      RF.suppressed_reason("contact19@careers.closedco.test", _AB, _DB) is not None)
check("an org-scope bar does NOT leak to a domain that merely ends in the same letters",
      RF.suppressed_reason("contact19@notclosedco.test", _AB, _DB) is None)
check("IDFC is barred org-wide (the registries.py gap)",
      RF.suppressed_reason("contact19@idfcfirstbank.com", _AB, _DB) is not None)
check("a banned org reaches the gate through registries.py",
      "registries.py" in (RF.suppressed_reason("a@deloitte.com", _AB, _DB) or ""),
      RF.suppressed_reason("a@deloitte.com", _AB, _DB))
check("a personal mailbox is barred", RF.suppressed_reason("a@gmail.com", _AB, _DB))
check("dot-variant of a suppressed address is barred",
      RF.suppressed_reason("s.t.o.p.request@acme.com", _AB, _DB) is not None)
check("+tag variant of a suppressed address is barred",
      RF.suppressed_reason("stoprequest+jobs@acme.com", _AB, _DB) is not None)

# -------------------------------------------------------------------------------------
# 5. THE GATE: a suppressed lead never reaches the craft step
# -------------------------------------------------------------------------------------
# Built by the module itself, not by hand: a hand-built map here would let main() drop a
# whole source while this section stayed green.
_HOLDS_AB = RF.merge_bars(barred, _AB)
check("merge_bars carries the holds buckets through",
      _HOLDS_AB.get("contact15@swiggy.in") == "on holds.json (bounced)", _HOLDS_AB)
check("merge_bars carries the suppression addresses through",
      "toldthemiwouldstop@acme.com" in _HOLDS_AB, sorted(_HOLDS_AB))
check("merge_bars lets the PROMISE reason win over a holds bucket",
      RF.merge_bars({"toldthemiwouldstop@acme.com": "capped"}, _AB)
      ["toldthemiwouldstop@acme.com"].startswith("PROMISE OF SILENCE"))
check("merge_bars refuses a None holds map",
      _raises(lambda: RF.merge_bars(None, _AB), ValueError))
check("merge_bars refuses a None suppression map",
      _raises(lambda: RF.merge_bars(barred, None), ValueError))

SCOPE = {"contact10@swiggy.in": "2026-11-03",                  # holds/bounced
         "contact11@flipkart.com": "2026-11-03",                # holds/capped
         "contact12@zomato.com": "2026-11-03",                 # holds/closed
         "stop.request@acme.com": "2026-11-03",                # do-not-mail + promise
         "told.them.i.would.stop@acme.com": "2026-11-03",      # promise of silence
         "someone@closedco.test": "2026-11-03",                # org scope
         "someone@idfcfirstbank.com": "2026-11-03",            # registry gap
         "someone@deloitte.com": "2026-11-03",                 # registries.py
         "contact13@swiggy.in": "2026-11-03",                   # replied -> survives
         "contact14@swiggy.in": "2026-11-03",                # review  -> survives
         "contact20@swiggy.in": "2026-11-03"}                 # clean   -> survives
sendable, blocked = RF.gate_scope(SCOPE, _HOLDS_AB, _DB)
_bto = {b["to"] for b in blocked}

for _who, _why in (("contact10@swiggy.in", "holds/bounced"),
                   ("contact11@flipkart.com", "holds/capped"),
                   ("contact12@zomato.com", "holds/closed"),
                   ("stop.request@acme.com", "do-not-mail"),
                   ("told.them.i.would.stop@acme.com", "promise of silence"),
                   ("someone@closedco.test", "org scope"),
                   ("someone@idfcfirstbank.com", "registry gap"),
                   ("someone@deloitte.com", "banned org")):
    check(f"gate removes {_why} lead from the rebuild scope",
          _who not in sendable and _who in _bto, sorted(sendable))

check("replied lead SURVIVES the gate", "contact13@swiggy.in" in sendable, sorted(sendable))
check("review lead SURVIVES the gate", "contact14@swiggy.in" in sendable, sorted(sendable))
check("clean lead SURVIVES the gate", "contact20@swiggy.in" in sendable, sorted(sendable))
check("exactly the three intended leads survive", len(sendable) == 3, sorted(sendable))
check("every barred row carries a reason",
      all((b.get("reason") or "").strip() for b in blocked), blocked)
check("every barred row keeps the drafted day",
      all(b.get("drafted") == "2026-11-03" for b in blocked), blocked)
check("the promise breach is reported AS a promise",
      any("PROMISE OF SILENCE" in b["reason"]
          for b in blocked if b["to"] == "told.them.i.would.stop@acme.com"), blocked)

# -------------------------------------------------------------------------------------
# 6. thread_promise: the one check no ledger can keep current
# -------------------------------------------------------------------------------------
_PL = "lead.person@acme.com"


def _tmsg(sender, text, to):
    return {"sender": sender, "to": to, "text": text}


check("a promise in MY outbound to this lead is caught",
      RF.thread_promise([_tmsg(ME, "Hi,\n\nso this will be the last one.", _PL)], _PL))
check("the caught promise is labelled a promise",
      "PROMISE OF SILENCE" in (RF.thread_promise(
          [_tmsg(ME, "I will make this the closing one.", _PL)], _PL) or ""))
check("a clean thread yields no promise",
      RF.thread_promise([_tmsg(ME, "Hi,\n\nfollowing up on the growth role.", _PL)],
                        _PL) is None)
check("a promise in THEIR message is not mine to keep",
      RF.thread_promise([_tmsg(_PL, "this will be the last one from me", ME)], _PL) is None)
# The case that actually EXERCISES the sender check. Addressed TO the lead, so the
# recipient check passes and only the sender check can reject it; the fixture above is
# addressed to ME, so there the two checks agree and neither is observable on its own.
check("a promise written by a THIRD PARTY to this lead is not mine to keep",
      RF.thread_promise([_tmsg("colleague@acme.com",
                               "so this will be the last one.", _PL)], _PL) is None)
check("a promise the LEAD sent to themselves is not mine to keep",
      RF.thread_promise([_tmsg(_PL, "so this will be the last one.", _PL)], _PL) is None)
check("my promise to SOMEBODY ELSE does not bar this lead",
      RF.thread_promise([_tmsg(ME, "so this will be the last one.", "other@acme.com")],
                        _PL) is None)
check("a dot-variant recipient still counts as this lead",
      RF.thread_promise([_tmsg(ME, "so this will be the last one.",
                               "lead.per.son@acme.com")], _PL))
check("an empty thread yields no promise", RF.thread_promise([], _PL) is None)
check("thread_promise reuses classify.PROMISE rather than its own regex",
      "PROMISE" in _inspect.getsource(RF.thread_promise)
      and "classify.PROMISE" in _inspect.getsource(RF.thread_promise))

# -------------------------------------------------------------------------------------
# 7. the live files load, and they are not empty
# -------------------------------------------------------------------------------------
_RS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state")
if os.path.exists(os.path.join(_RS, "holds.json")):
    _rb, _rn = RF.load_holds(os.path.join(_RS, "holds.json"))
    check("the live holds.json parses and bars a four-figure set", len(_rb) > 1000, len(_rb))
    check("the live review bucket bars nobody",
          not any(v == "review" for v in _rb.values()))
if os.path.exists(os.path.join(_RS, "suppression.json")):
    # The committed data itself, section by section: emptying any one of these would
    # narrow the live gate without changing a line of code.
    with open(os.path.join(_RS, "suppression.json"), encoding="utf-8") as _f:
        _live = _json.load(_f)
    check("live suppression.json has do-not-mail addresses",
          len(_live.get("do_not_mail") or {}) > 100, len(_live.get("do_not_mail") or {}))
    check("live suppression.json has promise-of-silence addresses",
          len(_live.get("promised_silence") or {}) > 50,
          len(_live.get("promised_silence") or {}))
    check("live suppression.json has org-scope domains",
          len(_live.get("do_not_mail_orgs") or {}) >= 5,
          len(_live.get("do_not_mail_orgs") or {}))
    check("live suppression.json has the registry-gap domains",
          len(_live.get("registry_gap_domains") or {}) >= 4,
          len(_live.get("registry_gap_domains") or {}))
    _sa, _sd = RF.load_suppression(os.path.join(_RS, "suppression.json"))
    check("the live suppression.json carries do-not-mail and promise addresses",
          len(_sa) > 100, len(_sa))
    check("the live suppression.json carries org and gap domains", len(_sd) >= 5, len(_sd))
    check("the live gate bars a known promised-silence org",
          RF.suppressed_reason("someone@airtel.com", _sa, _sd) is not None)

# -------------------------------------------------------------------------------------
# 7b. tools/build_suppression.py: the parse rules that FILL the gate
# -------------------------------------------------------------------------------------
# suppression.json is only as good as the parse that builds it, and the prior art
# (gate_review_holds.py) narrowed the gate four separate ways. Each is pinned here.
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import build_suppression as BS

_REG = os.path.join(_FIXDIR, "registry.md")
with open(_REG, "w", encoding="utf-8") as f:
    f.write("""---
name: do-not-mail-registry
---
Format: `address-or-org:domain | mailbox(A/B/both) | reason | date-added`

## REGISTRY

alpha.one@acme.com | A | CAP3 at 6 outbound | 2026-08-03
beta.two@acme.com | B | TONE_FLAG | 2026-08-04
gamma.three@acme.com | both | STOP_REQUEST | 2026-08-05
org:closedco.com | both | NO_OPENINGS_ORG until 2027-02-11 | 2026-08-18
org:twinone.com + org:twintwo.com | both | CAP3 ORG-WIDE | 2026-08-20
contact21@idfcfirst.bank.in (+ @idfcfirstbank.com) | A | REJECTION | 2026-09-09
this line has | only one pipe
| not | a | registry row either
""")

_da, _do, _ok = BS.parse_do_not_mail(_REG)
check("builder: registry file was found", _ok)
check("builder: a mailbox-A row is parsed", "alphaone@acme.com" in _da, sorted(_da))
check("builder: a mailbox-B row is parsed", "betatwo@acme.com" in _da, sorted(_da))
check("builder: a mailbox-BOTH row is parsed (the [AB] regex missed these)",
      "gammathree@acme.com" in _da, sorted(_da))
check("builder: an org: row becomes a domain bar (the old regex needed an @)",
      "closedco.com" in _do, sorted(_do))
check("builder: a row naming TWO orgs yields both domains",
      "twinone.com" in _do and "twintwo.com" in _do, sorted(_do))
check("builder: an org: row is NOT also stored as an address",
      not any("closedco.com" in a for a in _da), sorted(_da))
check("builder: a parenthesised second address is expanded",
      "contact22@idfcfirstbank.com" in _da, sorted(_da))
check("builder: the primary of that row is kept too",
      "contact22@idfcfirst.bank.in" in _da, sorted(_da))
check("builder: addresses are canonicalised",
      all("." not in a.split("@")[0] for a in _da), sorted(_da))
check("builder: prose and malformed rows are ignored", len(_da) == 5, sorted(_da))
check("builder: a missing registry reports not-found instead of crashing",
      BS.parse_do_not_mail(os.path.join(_FIXDIR, "nope.md"))[2] is False)

# parse_promises reads three files out of STATE; point STATE at the fixtures.
_BS_STATE = BS.STATE
BS.STATE = os.path.join(_FIXDIR, "state")
os.makedirs(BS.STATE, exist_ok=True)
with open(os.path.join(BS.STATE, "_promises.json"), "w", encoding="utf-8") as f:
    _json.dump({"addresses": [
        {"address": "Object.Shape@acme.com", "date": "2026-08-18",
         "promise": "so it will be the last one"},      # 57 of 59 live rows are objects
        "bare.string@acme.com",                          # and 2 are bare strings
        12345,                                           # junk must not crash the build
    ]}, f)
with open(os.path.join(BS.STATE, "promise_rescan_2026-10-13.json"), "w",
          encoding="utf-8") as f:
    _json.dump({"added_to_closed": ["rescan.closed@acme.com"],
                "already_held": ["rescan.held@acme.com"]}, f)
with open(os.path.join(BS.STATE, "_recraft_skips.json"), "w", encoding="utf-8") as f:
    _json.dump([{"to": "skip.promise@acme.com", "why": "PROMISE OF SILENCE. 12 Aug."},
                {"to": "skip.other@acme.com", "why": "tone flag, needs a rewrite"},
                {"to": "skip.mentions@acme.com",
                 "why": "AT CAP. 6 sent. Separately trips the prior-promise-of-silence "
                        "gate: the 11 Aug mail said this is where it ends."}], f)
# The promisor-side files must NOT be read: they record promises OTHER PEOPLE made to
# Gaurav, keyed on promisor_email. Those are leads to chase, the opposite of a bar.
for _f in ("_promises_sendable.json", "_promises_unkept.json", "_promises_found.json",
           "_promises_verified.json", "_promises_final.json"):
    with open(os.path.join(BS.STATE, _f), "w", encoding="utf-8") as f:
        _json.dump([{"promisor_email": "contact23@bigbasket.com",
                     "verdict": "UNKEPT"}], f)

_pr, _srcs = BS.parse_promises()
check("builder: an OBJECT promise row is read (57 of 59 live rows, all missed before)",
      "objectshape@acme.com" in _pr, sorted(_pr))
check("builder: the promise quote is kept as evidence",
      "last one" in _pr.get("objectshape@acme.com", ""), _pr.get("objectshape@acme.com"))
check("builder: a BARE STRING promise row is still read",
      "barestring@acme.com" in _pr, sorted(_pr))
check("builder: junk in addresses[] does not crash the build", len(_pr) >= 4)
check("builder: promise_rescan added_to_closed is read",
      "rescanclosed@acme.com" in _pr, sorted(_pr))
check("builder: promise_rescan already_held is read",
      "rescanheld@acme.com" in _pr, sorted(_pr))
check("builder: a recraft skip naming a PROMISE is read",
      "skippromise@acme.com" in _pr, sorted(_pr))
check("builder: a recraft skip with no promise in it is left out",
      "skipother@acme.com" not in _pr, sorted(_pr))
# DELIBERATELY tolerant, and this is the case that decides the rule. Of the 14 live skip
# reasons that mention a promise, 8 mention it MID-STRING -- "PRIOR PROMISE OF SILENCE",
# "Separately trips the prior-promise-of-silence gate", "Prior promise of silence,
# blocked" -- so anchoring the match to the start of the string would miss most of them.
# Over-suppressing one lead costs a repair; under-suppressing one costs a breach the
# recipient can see quoted on their own screen.
check("builder: a skip that mentions a promise mid-reason is STILL suppressed",
      "skipmentions@acme.com" in _pr, sorted(_pr))
check("builder: the promisor-side _promises_* files are NOT treated as suppressions",
      "contact24@bigbasket.com" not in _pr, sorted(_pr))
check("builder: only the three promise-of-silence sources are read",
      not any("sendable" in s or "unkept" in s or "verified" in s for s in _srcs), _srcs)
BS.STATE = _BS_STATE

check("builder: the registry-gap domains cover IDFC org-wide",
      any("idfc" in d for d in BS.REGISTRY_GAP_DOMAINS), BS.REGISTRY_GAP_DOMAINS)

# -------------------------------------------------------------------------------------
# 8. STRUCTURE: the gate must stand BEFORE the craft and the draft create
# -------------------------------------------------------------------------------------
# These are the orderings that make the gate a gate. A later edit that moves a check below
# the craft, or drops it, leaves the module as it was found on 2026-10-22.
_m = _inspect.getsource(RF.main)


def _before(hay, a, b):
    """True when `a` is present in `hay` AND comes before `b`.

    find, not index: when a call has been deleted outright this must read as one named
    failure, not a traceback that buries which assertion broke.
    """
    i, j = hay.find(a), hay.find(b)
    return i >= 0 and j >= 0 and i < j


check("main() loads holds before it opens the mailbox",
      _before(_m, "load_holds(", "Gmail()"))
check("main() loads suppression.json before it opens the mailbox",
      _before(_m, "load_suppression(", "Gmail()"))
check("main() gates the scope before it crafts",
      _before(_m, "gate_scope(", "craft_template("))
check("main() gates the scope before it creates a draft",
      _before(_m, "gate_scope(", "create_reply_draft("))
check("main() checks the thread for a promise before it crafts",
      _before(_m, "thread_promise(", "craft_template("))
check("main() re-checks suppression at the craft step (defence in depth)",
      _before(_m, "suppressed_reason(", "craft_template("))
check("main() builds its bar map with merge_bars, not inline",
      "merge_bars(" in _m)
check("main() hands BOTH maps to the gate",
      "gate_scope(scope, addr_bars, domain_bars)" in _m)
check("main() hands BOTH maps to the craft-step re-check",
      "suppressed_reason(lead, addr_bars, domain_bars)" in _m)
check("main() aborts when a gate source cannot be read",
      "return 4" in (_m[_m.find("load_holds("):_m.find("Gmail()")]
                     if _before(_m, "load_holds(", "Gmail()") else ""))

import shutil as _shutil
_shutil.rmtree(_FIXDIR, ignore_errors=True)
check("the fixture files are cleaned up", not os.path.exists(_FIXDIR))

print(f"\n{'ALL TESTS PASSED' if not FAILS else str(len(FAILS)) + ' FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
