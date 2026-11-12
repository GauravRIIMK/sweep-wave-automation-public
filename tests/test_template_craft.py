# -*- coding: utf-8 -*-
"""The zero-API crafter must pass the SAME gate the model's output must pass.

Run: python tests/test_template_craft.py

This suite is exhaustive rather than sampled, because the whole claim of a template is
that it passes BY CONSTRUCTION. A sampled test would let one bad variant reach real
people the way "a nudge in their direction" nearly did: it tripped the banned follow-up
vocabulary and the emotional-debt rule, and it would have gone to one recipient in four.
"""
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import template_craft as T, validate

FAILS = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  <-- {extra}"))
    if not cond:
        FAILS.append(name)


# --- exhaustive: every combination this module can emit -----------------------------
bad, lens, n = collections.Counter(), [], 0
for op in T.OPENERS:
    for ai, a in enumerate(T.POINTERS):
        for bi, b in enumerate(T.POINTERS):
            if ai == bi:
                continue
            for ask in T.ASKS:
                for cl in T.CLOSERS:
                    body = (f"Hi Trisha,\n\n{op.format(month='September', org='Acme')}\n\n"
                            f"- {a}\n- {b}\n\n{ask} {cl}\n\n{T.SIGNATURE}")
                    n += 1
                    ok, reasons = validate.validate(body, "Trisha", "p@acme.com", {}, "prior note")
                    lens.append(len(body.split()))
                    if not ok:
                        for r in reasons:
                            bad[r[:80]] += 1
check(f"every one of {n} combinations passes the validator", not bad,
      f"{sum(bad.values())} failures, e.g. {list(bad)[:2]}")
check("combinations() reports the real count", n == T.combinations(), f"{n} vs {T.combinations()}")

lens.sort()
check("no combination exceeds the 120-word ceiling", lens[-1] <= 120, f"max {lens[-1]}")
check("none is under the 70-word floor", lens[0] >= 70, f"min {lens[0]}")

# --- determinism, which is what makes a re-run safe ---------------------------------
a1 = T.render("trisha.n@swiggy.in", "Trisha", "Swiggy", "September")
a2 = T.render("trisha.n@swiggy.in", "Trisha", "Swiggy", "September")
check("same lead renders identically every time", a1 == a2)
check("case does not change the draw",
      T.render("TRISHA.N@SWIGGY.IN", "Trisha", "Swiggy", "September") == a1)

# --- variety, which is a SAFETY property: identical mail at volume is a spam signal --
bodies = [T.render(f"person{i}@acme.com", "Trisha", "Acme", "September") for i in range(400)]
uniq = len(set(bodies))
check("400 recipients get near-unique bodies", uniq >= 380, f"only {uniq} distinct")
top = collections.Counter(bodies).most_common(1)[0][1]
check("no single body dominates the population", top <= 4, f"one body used {top} times")

# --- the two pointers must never be the same sentence twice -------------------------
same = 0
for i in range(2000):
    p1, p2 = T._two_pointers(f"x{i}@acme.com")
    if p1 == p2:
        same += 1
check("a mail never repeats the same pointer twice", same == 0, f"{same} duplicates")

# --- contract with the caller -------------------------------------------------------
r = T.craft_template("x@acme.com", "Trisha", "Acme", [{"text": "prior"}], "September")
check("returns the craft.craft() contract",
      set(("ok", "body", "attempts", "reasons", "source")) <= set(r), str(sorted(r)))
check("labels itself so the report can tell it apart", r.get("source") == "template")
check("costs nothing: attempts is zero", r.get("attempts") == 0)

r2 = T.craft_template("x@acme.com", "", "Acme", [], "September")
check("refuses when the first name is unusable", not r2["ok"] and r2["body"] is None)
r3 = T.craft_template("x@acme.com", "Dr", "Acme", [], "September")
check("refuses a two-character name rather than guessing", not r3["ok"])

# --- every number must be grounded in the canonical fact sheet ----------------------
import re
from pipeline import registries as R
NUM = re.compile(r"\d+(?:\.\d+)?")
ground = set(NUM.findall(R.CANDIDATE_FACTS.replace(R.PHONE, " ")
                         .replace(R.REQUIRED_SIGNATURE, " ")))
ungrounded = set()
for p in T.POINTERS:
    for num in NUM.findall(p):
        if num not in ground:
            ungrounded.add(num)
check("no pointer cites a number absent from the fact sheet", not ungrounded, str(ungrounded))

banned = [p for p in T.POINTERS + T.ASKS + T.OPENERS + T.CLOSERS
          if "\u2014" in p or "\u2013" in p]
check("no em or en dashes anywhere in the variants", not banned, str(banned[:2]))
stale = [p for p in T.POINTERS if "Rs 15 Cr" in p]
check("the Rs 15 Cr 10x error appears nowhere", not stale)

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
