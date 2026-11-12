# -*- coding: utf-8 -*-
"""Deterministic, zero-API follow-up copy that passes the gate by construction.

WHY THIS EXISTS
---------------
On 19, 20 and 21 September the Anthropic credit balance ran out and the daily run
produced ZERO drafts three days running. The pipeline degraded correctly, in the sense
that it kept classifying and refused to spend, but a job-search engine that stops
entirely when a balance hits zero is not resilient, it is merely polite about failing.

What makes a template honest here is a change the OWNER asked for on 2026-09-22: these
mails are now JOB AGNOSTIC. No role is named, no requisition, no posting. `open_roles`
is not even passed to the model any more. Strip that out and look at what actually
varies per recipient:

    their first name, their organisation, the month he last wrote, and which two
    pointers from a fixed, verified fact sheet best fit them.

That is slot-filling, not writing. The model was being asked to re-derive the same mail
1,500 times and charged for the privilege, and it failed its own validator often enough
that 6 of 11 recorded drafts needed a second call.

WHAT THIS IS NOT
----------------
It is not a quality upgrade. A model reading the thread can catch things a template
never will: that they already said no, that the last message was warm, that their team
was just reorganised. It is a floor, not a ceiling. Use it when the model is unavailable
or when its draft was rejected, and prefer the model when it works.

TONE, which is the whole reason this is not a mail merge
--------------------------------------------------------
The owner's instruction is that EVERY recipient is written to as though they were
CXO-level, whatever their title. In a template that is not a per-call instruction to be
re-litigated and half-obeyed; it is a property of the sentences themselves, written once
and checked once. Every variant here is:

  - short, because brevity is the respect: about twenty seconds to read
  - specific about HIS work, never flattering about theirs
  - asking for one thing, easy to ignore, with no expectation attached
  - free of any claim about their company, since inventing one is unrecoverable

VARIETY IS A SAFETY FEATURE, NOT A FLOURISH
-------------------------------------------
Nine hundred people receiving a byte-identical mail is a spam signal and an insult.
Variants are chosen by a stable hash of the recipient's address, so the same person
always gets the same mail (re-running is idempotent) while the population is spread
across roughly seven thousand combinations.

Every number used below is a token present in registries.CANDIDATE_FACTS, because
validate.py refuses any figure it cannot ground there.
"""
import hashlib
import re

from . import registries as R
from . import validate

# Openers. Each carries a NEUTRAL back-reference, which the doctrine allows exactly once,
# and never names their silence or counts his own mails.
OPENERS = [
    "I wrote in {month} about growth and strategy work at {org}, and the interest holds.",
    "I wrote in {month} about growth and strategy roles at {org}. Short note to stay in touch.",
    "My note in {month} was about growth and strategy at {org}, and that is still where I am aimed.",
    "I wrote in {month} about growth and strategy work at {org}. A shorter version of it here.",
    "Back in {month} I wrote about growth and strategy at {org}. Still the direction I am set on.",
    "I wrote in {month} about growth and strategy roles at {org}, and wanted to leave one clearer line.",
]

# Pointers. Verified claims only, past tense where the factbank demands it, and each one
# stands alone so any pair reads sensibly. Roughly 22 to 30 words each: two of them plus
# an opener, the ask and the signature lands inside the 120-word ceiling.
POINTERS = [
    "Managed station P&L, quality and delivery SLAs across about 50 cloud kitchens in 4 cities "
    "at Blinkit Bistro, until I left in July.",

    "Led go to market for 38 dark store launches at Blinkit, built on a hyperlocal site "
    "selection framework running on 500+ data points.",

    "Built a referral engine at upGrad from zero to Rs 1.5 Cr in four months, designing the "
    "loop rather than running one-off campaigns.",

    "Doubled daily active users across 5 cities at Blinkit and cut marketing costs 40%, which "
    "is the same trade every growth team argues about.",

    "Cut churn 12% at Blinkit with behaviour-driven re-engagement, and built the real-time DAU "
    "and MTU dashboard the decisions ran on.",

    "Ran marketing program operations at Thoughtworks on Marketo and Demandbase, with AI "
    "assisted workflows that cut manual effort about 85 percent.",

    "SQL and Python are hands-on for me: dark store site selection, churn cohorts and funnel "
    "analysis, so the numbers behind a call are mine to defend.",

    "Closed 40% of a flagged quality gap in 2.5 weeks at Blinkit Bistro, which was mostly a "
    "question of what to stop doing rather than what to add.",

    # The figure that used to sit here was a former employer's internal number and it
    # shipped in roughly a fifth of all renders. The discipline is what sells; the
    # percentage is what tells a competitor he would discuss theirs too.
    "I ran the cannibalization work at Blinkit Bistro that stopped a launch, once its "
    "orders proved to be borrowed from a store we already had.",

    "Nearly five years across quick commerce and ed-tech, an IIM Kozhikode MBA, and based in "
    "Gurgaon.",
]

# The ask. Two variants here were REJECTED by the gate on first run and are recorded so
# they are not reintroduced: "a nudge in their direction" tripped the banned follow-up
# vocabulary list ("nudge"), and "would mean a lot" / "would help more than anything else"
# tripped T02_GREAT_DEAL, which exists to stop a small favour being dressed as a large
# emotional debt. 110 of 400 test renders failed on that one variant alone.
#
# Fixed in substance by instruction: a pointer to whoever owns hiring for the
# function, phrased so it also covers that person being them. ONE SENTENCE each, because
# the gate counts asks per sentence, and never "X or Y", which is its own blocking rule.
ASKS = [
    "If you could point me to whoever owns growth and strategy hiring on your side, even if "
    "that turns out to be you, I would be grateful.",

    "A steer towards whoever looks after growth and strategy hiring with you, yourself "
    "included, would be useful.",

    "If you know who owns growth and strategy hiring there, including you, a pointer would be "
    "genuinely useful.",

    "Whoever holds growth and strategy hiring on your side, and it may well be you, I would "
    "welcome an introduction.",
]

CLOSERS = ["Thank you for your time.", "Thank you.", "Grateful either way.",
           "Thank you for reading."]

SIGNATURE = f"Warm regards,\nGaurav Rathore\n{R.PHONE}\nLinkedIn: {R.REQUIRED_SIGNATURE}"


def _pick(seed, n, salt):
    """Stable index from the address. Same person, same mail, every run."""
    h = hashlib.sha256((salt + "|" + seed.lower()).encode("utf-8")).hexdigest()
    return int(h[:8], 16) % n


def _two_pointers(lead):
    """Two DIFFERENT pointers, chosen stably. The second is offset by a co-prime stride so
    the pair varies independently of the first, instead of always being neighbours."""
    n = len(POINTERS)
    a = _pick(lead, n, "p1")
    stride = 1 + _pick(lead, n - 1, "p2")
    b = (a + stride) % n
    return POINTERS[a], POINTERS[b]


def render(lead, first, org, month):
    """The body. Deterministic for a given (lead, first, org, month)."""
    opener = OPENERS[_pick(lead, len(OPENERS), "op")].format(
        month=month or "recent weeks", org=org or "your team")
    p1, p2 = _two_pointers(lead)
    ask = ASKS[_pick(lead, len(ASKS), "ask")]
    closer = CLOSERS[_pick(lead, len(CLOSERS), "cl")]
    return (f"Hi {first},\n\n{opener}\n\n- {p1}\n- {p2}\n\n{ask} {closer}\n\n{SIGNATURE}")


def craft_template(lead, first, org, msgs, month):
    """Same contract as craft.craft(): {ok, body, attempts, reasons, source}.

    Runs the SAME validator the model's output must pass. A template that cannot clear
    the gate is a bug in the template, and it must fail loudly here rather than quietly
    ship worse copy than the model would have.
    """
    if not first or len(first.strip()) < 3:
        return {"ok": False, "body": None, "attempts": 0, "source": "template",
                "reasons": ["no usable first name"]}
    body = render(lead, first.strip(), org, month)
    thread_text = " ".join((m.get("text") or "") for m in (msgs or []))
    ok, reasons = validate.validate(body, first.strip(), lead, {}, thread_text)
    return {"ok": ok, "body": body if ok else None, "attempts": 0,
            "source": "template", "reasons": reasons}


def combinations():
    """How many distinct mails this can produce. Used by the tests and worth knowing:
    identical mail at volume is a spam signal, so the spread is the safety property."""
    n = len(POINTERS)
    return len(OPENERS) * n * (n - 1) * len(ASKS) * len(CLOSERS)
