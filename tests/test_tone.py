# -*- coding: utf-8 -*-
"""Tone and voice doctrine gate: it must catch bad copy AND leave good copy alone.

A gate that blocks everything is as useless as one that blocks nothing, so this suite is
deliberately two-sided. The negative controls are real mails that were actually sent.

Run: python -m tests.test_tone
"""
import os, re as _re_mod, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import tone, validate, registries as R

FAILS = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  <-- {extra}"))
    if not cond:
        FAILS.append(name)


SIG = f"\n\nWarm regards,\nGaurav Rathore\n{R.PHONE}\nLinkedIn: {R.REQUIRED_SIGNATURE}"

CLEAN = """Hi Trisha,

Thank you for the time my earlier note took.

One thing has changed since I last wrote: I left Blinkit Bistro in July, so I am able to
move quickly now.

Where I have carried a number:

- Until July, Senior Manager, Growth Operations at Blinkit Bistro, running the P&L and
growth across roughly 50 pods in four cities
- At upGrad, a referral engine from zero to Rs 1.5 Cr in four months, and go to market for
38 launches

If a growth or GTM seat opens near you, I would be grateful for a pointer to whoever owns it.

Thank you.""" + SIG

print("\n== tone gate: version and load ==")
check("rules load and report a version", bool(tone.rules_version()), tone.rules_version())
check("vendored version is 1.5.1 (re-vendor if the canonical file moves)",
      tone.rules_version() == "1.5.1", tone.rules_version())

print("\n== negative control: clean copy must PASS ==")
bl, wn = tone.tone_violations(CLEAN)
check("a doctrine-compliant mail is not blocked", bl == [], f"blocked={bl}")

print("\n== positive controls: each defect must BLOCK ==")
CASES = [
    ("release tail (BANNED since 2026-08-26, was once required)",
     "If none of this is relevant, no reply is needed."),
    ("counts his own mails (ordinal)", "This is my fourth note on this role."),
    ("counts his own mails (cardinal)", "I have written to you twice already."),
    ("announces finality", "This is the last note you will get from me."),
    ("names their silence", "I have not yet heard back from you."),
    ("demands a rejection", "Even a one line no would genuinely help me."),
    ("debt framing", "I do not want to become a nuisance about this."),
    ("emotional debt ('great deal')", "A pointer would help a great deal."),
    ("expectation framing", "Please revert at your earliest convenience."),
    ("restates the credential block", "Where I am today: I run growth operations."),
    ("availability as a call to action", "I am available immediately and can start at once."),
    ("P.S. second hook", "P.S. I also shipped three AI tools this year."),
    ("em dash", "I left in July \u2014 so the search is real now."),
    ("Rs 15 Cr factual error", "I took the referral engine to Rs 15 Cr in four months."),
    ("stacked ask joined by 'or'",
     "Would you refer me for this role, or point me to whoever owns the hire?"),
]
for label, snippet in CASES:
    body = CLEAN.replace("Thank you.", snippet + "\n\nThank you.", 1)
    bl, wn = tone.tone_violations(body)
    check(f"blocks: {label}", bool(bl), f"snippet={snippet[:50]!r} blocked={bl}")

# The generic salutation is a GREETING defect, so it belongs at the start of the body.
# T07_HI_THERE is anchored with ^ and declares flags "i" (no MULTILINE) precisely so that
# "hi there" appearing mid-sentence cannot false-positive. Test it where it actually occurs.
bl, _ = tone.tone_violations("Hi there,\n\nThank you for your time." + SIG)
check("blocks: generic salutation at the top of the body", bool(bl), f"blocked={bl}")
bl, _ = tone.tone_violations(CLEAN.replace("Thank you.",
                                           "I said hi there once before.\n\nThank you.", 1))
check("does NOT false-positive on 'hi there' mid-sentence", not bl, f"blocked={bl}")

print("\n== the gate is wired into validate() ==")
ok, reasons = validate.validate(CLEAN, "Trisha", "trisha.hegde@acme.com",
                                research={"confidence": "low"}, thread_text="")
check("clean mail passes full validate()", ok, f"reasons={reasons}")

dirty = CLEAN.replace("Thank you.", "If nothing here fits, no reply is needed.\n\nThank you.", 1)
ok2, reasons2 = validate.validate(dirty, "Trisha", "trisha.hegde@acme.com",
                                  research={"confidence": "low"}, thread_text="")
check("release tail is rejected by validate(), not just by tone_violations",
      not ok2 and any("S03" in r or "release" in r.lower() for r in reasons2),
      f"ok={ok2} reasons={reasons2}")

print("\n== a broken rules file must fail CLOSED ==")
saved = tone._CACHE
try:
    tone._CACHE = ("x", [("BOOM", None, "block", {}, "m")])   # None regex -> AttributeError
    ok3, reasons3 = validate.validate(CLEAN, "Trisha", "trisha.hegde@acme.com",
                                      research={"confidence": "low"}, thread_text="")
    check("a tone gate that cannot run REFUSES the mail",
          not ok3 and any("tone gate failed" in r for r in reasons3), f"reasons={reasons3}")
finally:
    tone._CACHE = saved

print("\n== regression: real sent mails must all still pass ==")
import io, json
CORPUS = (r"C:\Users\GAURAV~1\AppData\Local\Temp\claude"
          r"\C--Users-Gaurav-Rathore-Downloads"
          r"\aa34c346-7b07-49a9-8592-26f0bda47ae9\scratchpad\repaired63.json")
if os.path.exists(CORPUS):
    corpus = json.load(io.open(CORPUS, encoding="utf-8"))
    blocked = [p["to"] for p in corpus if tone.tone_violations(p["body"])[0]]
    check(f"all {len(corpus)} mails sent 2026-09-10 still pass the gate",
          not blocked, f"blocked={blocked[:5]}")
else:
    print("  SKIP  sent-corpus regression (corpus not on this machine)")

print("")
print("== 1.5.1 regressions: the two misses from the 2026-10-20 batch ==")
# Both were reproduced as MISSES against rules 1.5.0 before the rules changed.
_bl, _wn = tone.tone_violations("I remain very interested in the role." + SIG, touch="followup")
check("T04_RESTATED_INTEREST warns on an adverb-broken restatement",
      any("T04_RESTATED_INTEREST" in str(x) for x in _wn), f"warn={_wn}")
check("that restatement warns rather than blocks, so a blessed clean mail still passes",
      not any("T04_RESTATED_INTEREST" in str(x) for x in _bl), f"blocked={_bl}")
_bl2, _wn2 = tone.tone_violations("A note from last week - the offer stands." + SIG, touch="followup")
check("T07_SPACED_HYPHEN warns on an ASCII hyphen used as a dash",
      any("T07_SPACED_HYPHEN" in str(x) for x in _wn2), f"warn={_wn2}")
_bl3, _wn3 = tone.tone_violations("I remain genuinely interested in the role." + SIG, touch="followup")
check("T04 block severity unchanged: remain genuinely interested is not BLOCKED",
      not any("T04_BOILERPLATE_CREDS" in str(x) for x in _bl3), f"blocked={_bl3}")

print("")
print("== structural rules: executed, not merely declared (fixed 2026-10-21) ==")
# Until this date the engine ran exactly ONE structural rule (S03). The other eleven were
# loaded, counted, and never evaluated. Every control below is PAIRED with a negative
# control, because a rule that fires on everything proves nothing.


def _ids(v):
    return sorted(set(str(x).split(":")[0] for x in v))


def _rep(body, touch="followup", **ctx):
    return tone.tone_report(body, touch=touch, ctx=ctx or None)


# --- S00: an empty own-part is a defect. Without quoted-history stripping the gate used
#     to grade a reply shell on the QUOTED party's prose and reported T02_IMPERATIVE_DEMAND
#     for a sentence the recipient wrote.
for _lbl, _shell in (("marker on line 1", "On Mon, 20 Oct 2026 at 10:00, P <p@x.com> wrote:\n> Will revert.\n"),
                     ("blank line first", "\n\nOn Mon, 20 Oct 2026 at 10:00, P <p@x.com> wrote:\n> Will revert.\n")):
    _r = _rep(_shell, touch="reply")
    check("S00 blocks an empty reply shell (%s)" % _lbl,
          "S00_EMPTY_BODY" in _ids(_r["blocks"]), _ids(_r["blocks"]))
_r = _rep("Hi P,\n\nThe charter opened this week." + SIG + "\n\nOn Mon, 20 Oct 2026 at 10:00, P <p@x.com> wrote:\n> Will revert.\n", touch="reply")
check("S00 silent when real text sits above the quote",
      "S00_EMPTY_BODY" not in _ids(_r["blocks"]) and _r["word_count"] > 0,
      "wc=%s %s" % (_r["word_count"], _ids(_r["blocks"])))

ONE_ASK = "Hi P,\n\nCould you point me to whoever owns growth hiring?" + SIG
TWO_ASK = ("Hi P,\n\nCould you point me to whoever owns growth hiring? "
           "Would you be able to share a view on the Bengaluru charter?" + SIG)

# --- S01 / S02: one ask on a normal touch, zero on a final, and never both at once
check("S01 blocks two asks on a followup",
      "S01_MAX_ASKS" in _ids(_rep(TWO_ASK)["blocks"]), _ids(_rep(TWO_ASK)["blocks"]))
check("S01 allows one ask on a followup",
      "S01_MAX_ASKS" not in _ids(_rep(ONE_ASK)["blocks"]), _ids(_rep(ONE_ASK)["blocks"]))
_rf = _rep(ONE_ASK, touch="final")
check("S02 blocks any ask on a final touch", "S02_FINAL_NO_ASK" in _ids(_rf["blocks"]))
check("S01 and S02 never double-report the same sentence",
      "S01_MAX_ASKS" not in _ids(_rf["blocks"]), _ids(_rf["blocks"]))

# --- S04: the new data point must be verbatim in the body, not merely asserted
NEWS = "Hi P,\n\nThe Bengaluru charter opened this week." + SIG
check("S04 blocks when new_info is absent from the body",
      "S04_FOLLOWUP_NEEDS_NEW_INFO" in _ids(_rep(NEWS, new_info="Swiggy posted a Senior Manager req")["blocks"]))
check("S04 silent when new_info appears in the body",
      "S04_FOLLOWUP_NEEDS_NEW_INFO" not in _ids(_rep(NEWS, new_info="The Bengaluru charter opened this week")["blocks"]))

# --- S05: do not re-send sentences the recipient has already read (min_words 8, max 1)
_s1 = "The Bengaluru growth charter opened this week for senior managers."
_s2 = "I have spent four years on retention and category profit work."
REPEATED = "Hi P,\n\n%s %s" % (_s1, _s2) + SIG
check("S05 blocks two long sentences reused from a prior mail",
      "S05_REPEAT_SENTENCES" in _ids(_rep(REPEATED, prior_bodies=["Hi P,\n\n%s %s" % (_s1, _s2)])["blocks"]))
check("S05 silent against an unrelated prior mail",
      "S05_REPEAT_SENTENCES" not in _ids(_rep(REPEATED, prior_bodies=["Hi P,\n\nNothing in common at all."])["blocks"]))

# --- S06: the ask must never grow across touches
check("S06 fires when the ask count grows 0 -> 1",
      "S06_ESCALATING_ASK" in _ids(_rep(ONE_ASK, prior_ask_count=0)["blocks"]) + _ids(_rep(ONE_ASK, prior_ask_count=0)["warns"]))
check("S06 silent when the ask count holds at 1",
      "S06_ESCALATING_ASK" not in _ids(_rep(ONE_ASK, prior_ask_count=1)["blocks"]) + _ids(_rep(ONE_ASK, prior_ask_count=1)["warns"]))

# --- S07: word budget, measured on content with the signature and quote removed
check("S07 blocks 130 words on a followup (cap 120)",
      "S07_MAX_WORDS" in _ids(_rep("Hi P,\n\n" + ("word " * 130) + "." + SIG)["blocks"]))
check("S07 allows 100 words on a followup",
      "S07_MAX_WORDS" not in _ids(_rep("Hi P,\n\n" + ("word " * 100) + "." + SIG)["blocks"]))
_RB = ("Hi P,\n\n" + ("word " * 150) +
       " https://jobs.smartrecruiters.com/SWIGGY/123-senior-manager-growth ." + SIG)
check("S07 grants the larger budget when a live requisition is named",
      "S07_MAX_WORDS" not in _ids(_rep(_RB)["blocks"]), _ids(_rep(_RB)["blocks"]))

# --- S08: hard cap on total touches per human
check("S08 blocks touch 7 against a cap of 6",
      "S08_TOUCH_CAP" in _ids(_rep(NEWS, touch_number=7)["blocks"]))
check("S08 allows touch 6", "S08_TOUCH_CAP" not in _ids(_rep(NEWS, touch_number=6)["blocks"]))

# --- S09: never mail a suppressed address
_SUP = tone.load_holds()
check("S09 suppression store loads from state/holds.json", bool(_SUP), _SUP)
if _SUP:
    check("S09 blocks an address in a suppressing bucket",
          "S09_SUPPRESSED" in _ids(_rep(NEWS, to=sorted(_SUP)[0])["blocks"]))
    check("S09 silent on an address nobody has mailed",
          "S09_SUPPRESSED" not in _ids(_rep(NEWS, to="nobody.new@example.org")["blocks"]))
check("S09 matches whole addresses, never substrings (lever.co must not block unilever.com)",
      tone.is_suppressed("someone@unilever.com", {"a@lever.co": "closed"}) is None)
check("replied and review are NOT treated as suppressions",
      set(tone.SUPPRESSING_HOLD_BUCKETS) == {"bounced", "capped", "closed"},
      tone.SUPPRESSING_HOLD_BUCKETS)

# --- applies_to was ignored entirely, so T04_BOILERPLATE_CREDS (BLOCK, scoped to
#     [followup, final]) fired on `initial` too: a first cold mail was blocked by a rule
#     written to stop RE-stating credentials.
CREDS = "Hi P,\n\nWhere I am today: I lead growth strategy for a D2C portfolio." + SIG
for _t, _want in (("initial", False), ("reply", False), ("followup", True), ("final", True)):
    check("applies_to honoured: T04_BOILERPLATE_CREDS %s on %s"
          % ("blocks" if _want else "stays silent", _t),
          ("T04_BOILERPLATE_CREDS" in _ids(_rep(CREDS, touch=_t)["blocks"])) == _want,
          _ids(_rep(CREDS, touch=_t)["blocks"]))

# --- line flattening: six patterns bound their span with [^.\n], and Gmail hard-wraps at
#     about 72 characters, so a wrap landing inside that span used to defeat the rule.
_WRAP = "Hi P,\n\nMy interest in the growth\ncharter has only grown." + SIG
_FLAT = "Hi P,\n\nMy interest in the growth charter has only grown." + SIG
check("a Gmail hard-wrap inside a bounded span no longer escapes the rule",
      bool(_rep(_WRAP)["blocks"]) and bool(_rep(_FLAT)["blocks"]),
      "wrapped=%s flat=%s" % (_ids(_rep(_WRAP)["blocks"]), _ids(_rep(_FLAT)["blocks"])))

# --- context-free rules must stay context-free: no ctx must never mean "passed"
_NOCTX = tone.tone_report(NEWS, touch="followup")
for _rid in ("S04_FOLLOWUP_NEEDS_NEW_INFO", "S05_REPEAT_SENTENCES", "S06_ESCALATING_ASK",
             "S08_TOUCH_CAP", "S09_SUPPRESSED", "S12_ROLE_TITLE_VERBATIM",
             "S10_ORG_LOAD", "S11_DRAFT_AGE"):
    check("%s is reported UNCHECKED rather than silently passed" % _rid,
          any(_rid in u for u in _NOCTX["unchecked"]), _NOCTX["unchecked"])
check("the 2-tuple still surfaces the unchecked set in warnings",
      any(str(w).startswith("UNCHECKED") for w in tone.tone_violations(NEWS)[1]))
check("tone_violations is still a 2-tuple for dispatch.py and validate.py",
      len(tone.tone_violations(NEWS)) == 2)

print("")
print("== the live send path must be unchanged by all of the above ==")
# dispatch.py and validate.py both call with touch="followup" and no context. If this
# count is not zero the fix has silently changed what the pipeline will send.
if os.path.exists(CORPUS):
    _c = json.load(io.open(CORPUS, encoding="utf-8"))
    _blk = [p["to"] for p in _c if tone.tone_violations(p["body"], touch="followup")[0]]
    check("0 of %d real sent mails block on the live followup path" % len(_c),
          not _blk, "blocked=%s" % _blk[:5])

print("")
print("== nothing below the sign-off may hide from the gate (review finding, 2026-10-22) ==")
# Ported faithfully from the canonical engine, every rule except FORMAT and FACT scanned
# only the text ABOVE the sign-off. A P.S. is prose the recipient reads, so a demand or a
# second ask placed there passed with ok=True and an empty block list. The regex rules and
# the ask counter now read the sender's entire own text. Measured on the 63 real mails:
# zero new blocks, identical word counts, identical ask counts.
FULLSIG = "\n\nThanks,\nGaurav Rathore\n+91 7888939968\nlinkedin.com/in/gauravrathore1007\n"
_PS = ("Hi Chirag,\n\n" + ("word " * 90) + "." + FULLSIG
       + "\nP.S. Even a one line no would help me a great deal.\n")
_ABOVE = ("Hi Chirag,\n\n" + ("word " * 90)
          + ". Even a one line no would help me a great deal." + FULLSIG)
_ps_ids, _above_ids = _ids(_rep(_PS)["blocks"]), _ids(_rep(_ABOVE)["blocks"])
check("a demand-for-rejection in a P.S. below the sign-off still blocks",
      "T02_DEMAND_REJECTION" in _ps_ids, _ps_ids)
check("it blocks on the SAME rules as when it sits above the sign-off",
      set(_above_ids).issubset(set(_ps_ids)), "above=%s ps=%s" % (_above_ids, _ps_ids))
# A second ask smuggled into a P.S. must still count against S01's budget of one.
# The prose must be long enough that the sign-off lands past split_signature's 45% floor,
# or content == own and the check cannot observe which region was counted. An earlier
# version of this test used a one-line body and passed with the fix reverted.
_LEAD = ("Hi A,\n\nThank you for the note last month about the Bengaluru charter and the "
         "quick commerce retention work your team has been shipping.\n\n"
         "Could you point me to whoever owns growth hiring?")
_PS_ASK = _LEAD + FULLSIG + "\nP.S. Would you be able to share a view on the charter?\n"
_r = _rep(_PS_ASK)
_own_ps = tone.strip_quoted_history(_PS_ASK)
_c_ps, _s_ps = tone.split_signature(_own_ps, tone._load()[5])
check("the P.S. test body actually exercises a split signature",
      _s_ps.startswith("Thanks,") and "P.S." not in _c_ps,
      "sig=%r" % _s_ps[:30])
check("an ask in a P.S. is counted, so two asks still trip S01",
      _r["ask_count"] == 2 and "S01_MAX_ASKS" in _ids(_r["blocks"]),
      "asks=%s %s" % (_r["ask_count"], _ids(_r["blocks"])))

# split_signature must not mistake a late prose "Thanks," for a sign-off. The canonical
# engine tests only the 45% floor; a line such as "Thanks, and congratulations on the
# Series F. Please revert with a clear yes or no." then becomes "signature" and its demand
# leaves `content`, under-counting the S07 word budget.
# The prose "Thanks," must itself land past the 45% floor, otherwise the old code would
# not have split there either and the check proves nothing. Here it sits at ~57%.
_PROSE_THANKS = ("Hi Chirag,\n\nThank you for reading earlier in the month, and for the note "
                 "about how the Minutes team is staffing its growth pod this quarter.\n\n"
                 "Thanks, and congratulations on the Series F. "
                 "Please revert with a clear yes or no.\n")
_own_pt = tone.strip_quoted_history(_PROSE_THANKS)
check("the prose 'Thanks,' in this body really is past the 45% floor",
      _own_pt.find("Thanks,") >= len(_own_pt) * 0.55,
      "at %.0f%%" % (100.0 * _own_pt.find("Thanks,") / len(_own_pt)))
check("a late prose 'Thanks,' is not treated as a sign-off",
      "T02_DEMAND_REJECTION" in _ids(_rep(_PROSE_THANKS)["blocks"]),
      _ids(_rep(_PROSE_THANKS)["blocks"]))
_c, _s = tone.split_signature(_own_pt, tone._load()[5])
check("the demand stays in content, so S07 counts it",
      "please revert" in _c.lower(), "content=%r sig=%r" % (_c[-40:], _s[:40]))

# ...but a real sign-off, alone on its line, must still be split off -- and this has to be
# observable THROUGH tone_report, not just by calling split_signature directly, or
# bypassing the split inside tone_report leaves the check green.
_SPLIT_BODY = "Hi A,\n\n" + ("word " * 40) + "." + FULLSIG
_r_split = _rep(_SPLIT_BODY)
_prose_words = len(_re_mod.findall(r"\b\w[\w'&%+/.-]*\b", "Hi A,\n\n" + ("word " * 40) + "."))
check("tone_report's word count excludes the signature block",
      _r_split["word_count"] == _prose_words,
      "wc=%s prose=%s preview=%r" % (_r_split["word_count"], _prose_words,
                                     _r_split["content_preview"][-40:]))
check("the signature itself is not reported as content",
      "7888939968" not in _r_split["content_preview"]
      and "linkedin" not in _r_split["content_preview"].lower(),
      _r_split["content_preview"][-50:])

print("")
print("== quoted history must be stripped from NON-empty bodies too ==")
# The only control written for this asserted S00 did not fire and word_count > 0, both of
# which hold even when the quote is NOT stripped. It could not see the fix it guarded.
_WITH_QUOTE = ("Hi P,\n\nThe charter opened this week." + SIG
               + "\n\nOn Mon, 20 Oct 2026 at 10:00, P <p@x.com> wrote:\n"
                 "> Please revert with a clear yes or no.\n> I owe you a reply.\n")
_r = _rep(_WITH_QUOTE, touch="reply")
check("the recipient's words are not charged to the sender",
      not _r["blocks"], _ids(_r["blocks"]))
check("the recipient's text is absent from the scanned region",
      "revert" not in _r["content_preview"].lower()
      and "owe you" not in _r["content_preview"].lower(),
      "content=%r" % _r["content_preview"])
# Measured, not asserted by eye: the same body with the quote left in is materially longer,
# which is what would inflate the S07 budget and charge their words to the sender. (Do not
# assert a small absolute word count here: on a short body the sign-off sits above the 45%
# floor, so split_signature correctly declines to split and the signature is still counted.)
_wc_stripped = _r["word_count"]
_wc_raw = len(_re_mod.findall(r"\b\w[\w'&%+/.-]*\b", _WITH_QUOTE))
check("stripping the quote materially lowers the counted length",
      _wc_stripped < _wc_raw, "stripped=%s raw=%s" % (_wc_stripped, _wc_raw))

print("")
print("== validate() must hand the recipient to the gate so S09 actually runs ==")
# The headline fix of the 2026-10-21 change lived entirely in validate.py, and NOTHING
# asserted it. Reverting `ctx={"to": lead_email}` left all 67 checks green while a
# suppressed lead became craftable again. pipeline/resend_failed.py reaches
# template_craft -> validate -> create_reply_draft with no holds check of its own, so on
# that path this is the only suppression guard standing.
_SUPP = tone.load_holds()
_CLEAN_BODY = "Hi Trisha,\n\nThe Bengaluru growth charter opened this week." + FULLSIG
if _SUPP:
    _addr = sorted(_SUPP)[0]
    _ok, _why = validate.validate(_CLEAN_BODY, "Trisha", _addr)
    check("validate() blocks a suppressed recipient via S09",
          any("S09_SUPPRESSED" in r for r in _why), _why[:3])
    # The doctrine exemption: someone who asked a question is owed an answer even at cap.
    _ok_r, _why_r = validate.validate(_CLEAN_BODY, "Trisha", _addr, touch="reply")
    check("validate(touch='reply') does NOT suppress the same address",
          not any("S09_SUPPRESSED" in r for r in _why_r), _why_r[:3])
    check("S09 is not scoped to reply in the rules file",
          "reply" not in (next(s for s in json.load(io.open(
              os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "pipeline", "tone_rules.json"), encoding="utf-8-sig"))["structural_rules"]
              if s["id"] == "S09_SUPPRESSED").get("applies_to") or []))
    _ok2, _why2 = validate.validate(_CLEAN_BODY, "Trisha", "nobody.new@example.org")
    check("validate() does not suppress an address nobody has mailed",
          not any("S09_SUPPRESSED" in r for r in _why2), _why2[:3])
else:
    print("  SKIP  validate()/S09 handover (suppression store unreadable)")
# touch must stay keyword-compatible: craft.py and template_craft.py call positionally.
check("validate() keeps its positional signature for craft.py and template_craft.py",
      validate.validate.__code__.co_varnames[:6]
      == ("body", "first_name", "lead_email", "research", "thread_text", "touch"),
      validate.validate.__code__.co_varnames[:6])

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
