# -*- coding: utf-8 -*-
"""Machine-checkable doctrine gate. A draft in this mailbox auto-sends, so a crafted mail
is only allowed out if it passes EVERY check here. Failures are returned as reasons so the
crafting step can retry once with the specific complaints, then hold for human review."""
import re
from . import registries as R
from . import tone

# Numbers a mail may contain without external grounding: the canonical career facts only.
# Bare "1", "5", "91" and "1007" were removed. They licensed any claim built from those
# digits, and "1007" was only ever a fragment of the sender's own email local part.
ALLOWED_NUMBERS = {"1.5", "200", "38", "50", "4.5", "7888939968", "91"}
NUM = re.compile(r"\d+(?:\.\d+)?")
DASHES = re.compile(r"[–—‒―]")          # en/em/figure/horizontal bar
SMART = re.compile(r"[‘’“”]")


def _sentences(t):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", t) if s.strip()]


def validate(body, first_name, lead_email, research=None, thread_text="", touch="followup"):
    """Return (ok: bool, reasons: [str]).

    `touch` selects the tone gate's severity column and decides which structural rules
    apply. It defaults to "followup", which is what every current caller sends: craft.py
    and template_craft.py only ever see ELIGIBLE leads, because classify.py routes a
    REPLY_DUE thread away from auto-drafting and make_replies.py builds reply drafts on its
    own path. Pass touch="reply" if that ever changes -- S09_SUPPRESSED deliberately does
    not apply to a reply, since someone who asked a question is owed an answer even at the
    touch cap, and grading a reply as a "followup" would suppress it.
    """
    reasons = []
    b = body or ""
    low = b.lower()

    if not b.strip():
        return False, ["empty body"]

    # 1. greeting: must be "Hi <FirstName>," exactly as the thread addresses them
    first_line = b.strip().splitlines()[0].strip()
    if not re.match(rf"^Hi {re.escape(first_name)}\s*,$", first_line):
        reasons.append(f"first line must be exactly 'Hi {first_name},' (got: {first_line[:60]!r})")
    if low.startswith("dear"):
        reasons.append("'Dear' is banned; use 'Hi'")

    # 2. punctuation doctrine
    if DASHES.search(b):
        reasons.append("contains an em/en dash")
    if SMART.search(b):
        reasons.append("contains smart quotes")

    # 3. banned follow-up vocabulary
    for p in R.BANNED_PHRASES:
        if p in low:
            reasons.append(f"banned phrase: {p!r}")

    # 3b. TONE AND VOICE DOCTRINE, all 19 rule families, machine-checked.
    #     BANNED_PHRASES above is a flat substring list and catches only the oldest
    #     offenders. The doctrine has moved well past it: the release tail is now BANNED
    #     rather than required (inverted 2026-08-26), and debt framing, ordinal
    #     self-counting, demanding a rejection, restating the credential block and
    #     expectation framing ("would help a great deal") are all blocking. Each family
    #     was added after a real mail went wrong, so they are enforced from the canonical
    #     rules file rather than paraphrased here.
    #     Verified 2026-09-10 against the 63 mails actually sent that day: 0 blocked,
    #     so this gate does not second-guess copy that already passed review.
    #     The recipient is passed through so S09_SUPPRESSED actually runs. It was dead
    #     until 2026-10-21 along with ten other structural rules, which meant a lead who
    #     had hard-bounced, hit the touch cap, or explicitly closed the door could still
    #     be crafted to and gated clean. `lead_email` was already a parameter here; nothing
    #     needed fetching, the context simply was not being handed over.
    #
    #     thread_text is deliberately NOT passed as S05_REPEAT_SENTENCES prior_bodies.
    #     craft.py's digest keeps only the last 3 messages at 600 chars each and labels
    #     both directions, so it is a partial, mixed-direction record. Feeding it would
    #     turn an honest UNCHECKED into a "checked" verdict against evidence that cannot
    #     support it, which is the exact failure mode this gate exists to prevent. S05
    #     stays UNCHECKED here until a caller can supply my own prior outbound bodies in
    #     full.
    try:
        tone_block, tone_warn = tone.tone_violations(
            b, touch=touch, ctx={"to": lead_email})
        reasons.extend(tone_block)
    except Exception as e:
        # A tone gate that fails open is worse than none: every report would claim the
        # mail was checked. Refuse the mail instead.
        reasons.append(f"tone gate failed to run ({e}); refusing rather than sending unchecked")

    # 4. factual guards
    for bad, good in R.FACT_GUARDS:
        if bad in low:
            reasons.append(f"factual error {bad!r}; correct form is {good}")

    # 4b. A former employer's INTERNAL figures, banned outright rather than merely
    #     ungrounded. Removing them from CANDIDATE_FACTS already makes the number check
    #     below reject the digits, but that is not enough on its own: a spelled-out form
    #     slips past a numeric test, and a later edit to the fact sheet would silently
    #     re-enable them. 127 of 712 queued drafts carried these before this gate existed.
    for bad, why in getattr(R, "BANNED_FIGURES", []):
        if bad in low:
            reasons.append(f"banned internal figure {bad!r} ({why}); keep the story, "
                           f"drop the number")

    # 5. signature integrity
    if R.REQUIRED_SIGNATURE not in b:
        reasons.append("missing canonical LinkedIn URL in signature")
    if R.PHONE not in b:
        reasons.append("missing phone in signature")
    if "Gaurav Rathore" not in b:
        reasons.append("missing name in signature")

    # 6. exactly one ask
    asks = b.count("?")
    if asks > 1:
        reasons.append(f"{asks} questions; doctrine allows one ask")

    # 7. length
    words = len(b.split())
    # 120, not 260, and the two numbers being different is how un-sendable mail got made.
    # tone_rules.json S07_MAX_WORDS BLOCKS a followup over 120 words, allowing 170 only
    # when the mail names a specific role. These mails are job agnostic by instruction, so
    # they forfeit that allowance. With the ceiling here at 260 the crafter cheerfully
    # produced 141 to 184 word drafts that passed validation and were then refused by the
    # send-time tone check: work done, spend spent, mail undeliverable. A validator looser
    # than the gate it feeds is not a validator, it is a delay.
    # The crafter gets one retry with these complaints, so tightening here makes it
    # self-correct rather than fail.
    if words > 120:
        reasons.append(f"too long ({words} words, max 120 for a job agnostic follow-up; "
                       f"cut the weaker pointer rather than compressing both)")
    if words < 70:
        reasons.append(f"too short ({words} words, min 70)")

    # 8. number grounding: every number must be canonical, or present in the research
    #    payload / existing thread. Blocks invented metrics and invented role IDs.
    #    Only the research FIELDS actually shown to the writer count as grounding. The
    #    whole dict must not be stringified: match_evidence and error text can contain
    #    stray numbers that would silently license an invented figure.
    res_ground = ""
    if isinstance(research, dict):
        vals = [research.get(k) for k in ("title", "team", "org_context", "hook")]
        vals += list(research.get("open_roles") or [])
        res_ground = " ".join(str(v) for v in vals if v)
    elif research:
        res_ground = str(research)
    facts = R.CANDIDATE_FACTS.replace(R.PHONE, " ").replace(R.REQUIRED_SIGNATURE, " ")
    ground = " ".join(filter(None, [res_ground, thread_text or "", facts])).lower()
    # Compare TOKENS, never substrings. A substring test against the grounding blob let
    # every digit run inside the phone number (39, 68, 88, 89, 93, 96, 99 ...) silently
    # license an invented metric such as "lifted activation 68 percent".
    ground_nums = set(NUM.findall(ground))
    # The signature block is boilerplate checked separately above; its digits (the phone,
    # and the "1" inside the LinkedIn slug) are not claims, so exclude it from the scan.
    body_scan = b.replace(R.PHONE, " ").replace(R.REQUIRED_SIGNATURE, " ")
    for n in set(NUM.findall(body_scan)):
        if n in ALLOWED_NUMBERS or n in ground_nums:
            continue
        if len(n) == 4 and n.startswith("20"):     # a year
            continue
        reasons.append(f"ungrounded number {n!r}")

    # 8b. a percentage or rupee figure must be canonical or verbatim in the grounding text
    for figure in re.findall(r"\d+(?:\.\d+)?\s*(?:percent|%|per cent)", low):
        num = NUM.search(figure).group(0)
        if num not in ground_nums and num not in ALLOWED_NUMBERS:
            reasons.append(f"ungrounded percentage {figure.strip()!r}")

    # 9. no flattery openers
    for p in ("i hope this finds you", "hope you are doing well", "hope this email finds",
              "i am a big fan", "i loved your", "impressive", "amazing work"):
        if p in low:
            reasons.append(f"flattery/filler: {p!r}")

    # 10. must not address the wrong person
    for other in re.findall(r"^Hi ([A-Z][a-z]+)", b, re.M):
        if other != first_name:
            reasons.append(f"greets {other!r} but the thread addresses {first_name!r}")

    # 10b. UNGROUNDED FAMILIARITY. A live dry run produced "given the IIM Kozhikode
    #      connection I thought it worth asking directly" to a stranger whose profile the
    #      researcher had explicitly FAILED to identify. Nothing in the fact sheet or the
    #      thread supported a shared connection; the model inferred one because both
    #      parties are Indian professionals. A fabricated relationship is the most
    #      damaging thing this pipeline could send, so any claim of shared history,
    #      common ground, or knowledge of the recipient is refused unless the research
    #      actually established their identity.
    conf = (research or {}).get("confidence") if isinstance(research, dict) else None
    if conf != "high":
        FAMILIARITY = [
            (r"\bthe .{0,30}connection\b", "implies a shared connection"),
            (r"\bwe both\b", "claims something in common"),
            (r"\bin common\b", "claims something in common"),
            (r"\bfellow \b", "claims shared affiliation"),
            (r"\balum\b", "claims shared alma mater"),
            (r"\bI (saw|noticed|read|came across|have been following)\b", "claims to have seen their work"),
            (r"\byour (recent|latest)\b", "claims knowledge of their recent work"),
            (r"\bsince you \b", "asserts a fact about the recipient"),
            (r"\bgiven (your|that you)\b", "asserts a fact about the recipient"),
            (r"\byour work on\b", "asserts a fact about the recipient"),
        ]
        tl = (thread_text or "").lower()
        for pat, why in FAMILIARITY:
            mm = re.search(pat, b, re.I)
            if not mm:
                continue
            # A claim the THREAD already supports is grounded: we wrote that thread, so if
            # an earlier mail established the connection, referencing it is fact, not
            # invention. Only unsupported claims are refused.
            words = [w for w in re.findall(r"[A-Za-z]{4,}", mm.group(0))
                     if w.lower() not in (
                         # structural glue in the pattern itself, not part of the claim
                         "your", "that", "given", "both", "recent", "latest", "work",
                         "fellow", "since", "have", "been", "following", "common", "saw",
                         "read", "noticed", "came", "across", "connection", "alum",
                         "alumni", "alumnus", "alumna", "with", "from", "this", "there")]
            if words and all(w.lower() in tl for w in words):
                continue
            reasons.append(f"ungrounded familiarity ({why}); research confidence is "
                           f"{conf or 'none'} and the thread does not support it")

    # 11. the greeting must not name a DIFFERENT person than the address suggests.
    #     Deliberately one-sided: corporate local parts are often opaque (lavanyaibrahim@ is
    #     "Rhea Nair"), so an unrecognisable local part proves nothing and must not
    #     block. Only a local part that clearly spells out another first name does.
    if lead_email and "@" in lead_email:
        local = lead_email.split("@", 1)[0].lower()
        tokens = [t for t in re.split(r"[._\-+0-9]+", local) if len(t) >= 4]
        fn = first_name.lower()
        if tokens and len(tokens) > 1:
            looks_like_names = all(t.isalpha() for t in tokens)
            matches = any(t.startswith(fn[:4]) or fn.startswith(t[:4]) for t in tokens)
            if looks_like_names and not matches:
                reasons.append(
                    f"greeting {first_name!r} matches no part of recipient {lead_email!r}")

    return (len(reasons) == 0), reasons
