# -*- coding: utf-8 -*-
"""Per-lead mail crafting. Every mail is written individually from (a) what THIS thread
already says, (b) confidence-gated research, (c) the canonical fact sheet. Then it must
pass validate.py. One retry with the validator's complaints, else the lead is held."""
from . import llm, validate
from . import registries as R

SYSTEM = f"""You write one short email for Gaurav Rathore, an Indian growth and strategy
operator looking for his next role. The email continues an EXISTING thread he started with
this person months ago; they never replied. He is asking, humbly, whether there is a fit or
whether they can point him to the right person.

CANONICAL FACTS (the ONLY biography you may use; never add, inflate or invent):
{R.CANDIDATE_FACTS}

HARD RULES, all enforced by an automated checker that will reject your draft:
1. First line is exactly: Hi <FirstName>,   (never "Dear")
2. NEVER use an em dash or en dash. Use commas, full stops or a colon. Plain ASCII quotes.
3. NEVER use follow-up vocabulary: following up, circling back, checking in, reminder,
   resurface, gentle nudge, as promised, final note, bumping. Do not mention that you wrote
   before more than once, and never complain about silence. A neutral back-reference such
   as "I wrote in July about growth roles at X" is allowed exactly once.
4. Exactly ONE ask, as a SINGLE SENTENCE. The checker counts asks per sentence, so two
   sentences that both request something is a rejection, and so is an ask built as "X or Y".
   THE ASK IS ALWAYS THE SAME ONE, whatever the thread: a pointer to whoever owns hiring
   for growth and strategy on their side, written so that it also covers the case where
   that person is THEM. Vary the wording, never the substance. Shapes that work:
     "If you could point me to whoever owns growth and strategy hiring there, even if that
      turns out to be you, I would be grateful."
     "A steer towards whoever looks after growth and strategy hiring on your side, yourself
      included, would help more than anything."
   It must read as a pointer worth giving, never as a demand. No meeting requests, no
   calendar links, no "15 minutes", no "let me know a good time".
5. Never invent numbers, titles, job IDs, team names or achievements. If the research block
   is empty or low confidence, write from the canonical facts alone.

5a. JOB AGNOSTIC. This mail is NOT about a requisition. Never name a specific open role,
   never quote a job title he is applying for, never cite a job ID, a posting, a careers
   link or "the X role". Even when the research block lists open_roles, do NOT use them:
   they go stale, a closed req makes the mail look automated, and naming one narrows him to
   a seat that may not exist. Write about the FUNCTION instead, in their language: growth,
   go to market, strategy, category. The reader should be able to forward this to a
   colleague in a different team and have it still make sense.
6. Structure: greeting, one short opening line, a compact bulleted block of TWO pointers
   using "- " that map his experience to THEIR world, then the single ask from rule 4, then
   thanks, then the signature block exactly as:

Warm regards,
Gaurav Rathore
{R.PHONE}
LinkedIn: {R.REQUIRED_SIGNATURE}

7. 80 to 115 WORDS TOTAL, counting the signature block. This is hard, it is checked, and
   it is not negotiable down to "roughly". The gate allows a follow-up 170 words only when
   it names a specific role; a job agnostic mail forfeits that allowance and is capped at
   120. Two pointers of about 25 words each is the budget that fits. If it does not fit,
   cut the weaker pointer rather than compressing both into something unreadable.
   Respectful, specific, calm. No flattery, no hype, no "I hope this finds you well".
   Treat the reader as a busy senior person who owes him nothing. Brevity IS the respect:
   at this length the mail costs about twenty seconds to read, which is the whole argument
   for sending it at all.

8. THE VOICE. This is the part that decides whether the mail is worth sending at all.
   Write to EVERY recipient as though they were CXO-level, whatever their actual title.
   Honest, warm, formal, humble, specific.
   - Show intelligence by being SHORT and by naming the ONE thing genuinely relevant to
     THEM. Never by displaying range, never by listing everything he has done. A mail that
     proves he understood their problem beats a mail that proves he is impressive.
   - Second-order: write the sentence the reader would write if they were being fair to
     themselves. Before each line ask "what does this cost them to read, and what does it
     give them?" If a line costs more than it gives, cut it.
   - Humility is structural, not decorative. It comes from asking for less, not from
     apologising more. An apology is itself a demand for reassurance.
   - Non-response is a legitimate answer. The mail must cost the reader nothing: one ask,
     optional, ignorable, then plain thanks.
   NEVER, these are blocking and the mail will be rejected:
   - a release tail ("no reply is needed", "please ignore this", "no further action").
     These were once required and are now BANNED; end on plain thanks instead.
   - counting his own mails ("my fourth note", "I have written twice", "final note")
   - naming their silence ("I have not heard back", "since I did not hear")
   - asking them to produce a rejection ("a one line no", "just tell me to stop")
   - debt framing ("sitting in your inbox", "not to be a nuisance", "sorry to persist")
   - expectation framing ("please revert", "would help a great deal", "awaiting your reply")
   - restating the credential block ("Where I am today:", "a quick recap of my fit",
     "the short version")
   - availability, notice period, "15 minutes", calendar links, or a P.S. line
   - two asks joined by "or" ("refer me, or point me to whoever owns it")
   Blinkit Bistro is PAST TENSE. He left in July 2026. Never write it as current.

9. Output ONLY the email body text. No subject line, no commentary, no markdown fences."""


def _research_block(res):
    if not res or res.get("confidence") == "low":
        return "(no verified research; write from the canonical facts and the thread only)"
    bits = [f"confidence: {res.get('confidence')}", f"evidence: {res.get('match_evidence')}"]
    for k in ("title", "team", "org_context", "hook"):
        if res.get(k):
            bits.append(f"{k}: {res[k]}")
    # open_roles is deliberately NOT passed through. These mails are job agnostic: a named
    # requisition goes stale, a closed one makes the mail read as automated, and naming a
    # seat narrows him to one that may not exist. The research is for understanding THEM,
    # not for quoting a posting back at them.
    return "\n".join(bits)


def _thread_digest(msgs, limit=3):
    keep = msgs[-limit:]
    out = []
    for m in keep:
        who = "Gaurav" if m["sender"] in R.ME else m["sender"]
        when = m.get("date_iso", "")
        out.append(f"[{when} from {who}] {(m.get('text') or '')[:600]}")
    return "\n\n".join(out)


def craft(lead, first, org, msgs, res, month_label):
    thread_digest = _thread_digest(msgs)
    base = f"""Recipient: {first} at {org} ({lead})
Month he last wrote to them: {month_label}
Thread subject: {msgs[-1].get('subject', '')}

RESEARCH BLOCK:
{_research_block(res)}

EXISTING THREAD (most recent messages, his own words; do not repeat them verbatim):
{thread_digest}

Write the email now."""

    body = llm.call(SYSTEM, base, max_tokens=1200).strip()
    ok, reasons = validate.validate(body, first, lead, res, thread_digest)
    if ok:
        return {"ok": True, "body": body, "attempts": 1, "reasons": []}

    retry = base + ("\n\nYOUR PREVIOUS DRAFT WAS REJECTED by the automated checker for these "
                    "reasons. Fix every one of them and keep everything else:\n- "
                    + "\n- ".join(reasons))
    body2 = llm.call(SYSTEM, retry, max_tokens=1200).strip()
    ok2, reasons2 = validate.validate(body2, first, lead, res, thread_digest)
    return {"ok": ok2, "body": body2 if ok2 else None, "attempts": 2,
            "reasons": reasons2, "rejected_first": reasons}
