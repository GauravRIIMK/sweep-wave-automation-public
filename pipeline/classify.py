# -*- coding: utf-8 -*-
"""Thread classification. The Gmail thread is the ONLY source of truth about a lead
(a local ledger is a cache, never an authority). Every decision here is deterministic
and explainable: each lead gets one verdict, one machine-readable state, and a
human-readable reason."""
import re
from . import registries as R

NEVER_MAIL = re.compile(R.NEVER_MAIL_PAT, re.I)
NON_HUMAN = re.compile(R.NON_HUMAN_PAT, re.I)
MACHINE_LOCAL = re.compile(R.MACHINE_LOCALPART, re.I)
BOUNCE = re.compile(R.BOUNCE_PAT, re.I)
PROMISE = re.compile(R.PROMISE_PAT, re.I)
GREET = re.compile(r"^(?:Hi|Hello|Hey|Dear)\s+([A-Z][A-Za-z.'\-]{1,25})\s*[,!\.]")
BADNAME = {"there", "team", "all", "sir", "madam", "folks", "everyone", "hiring", "recruiter"}

# Verdict states that are permanent: the lead should leave the pool for good.
TERMINAL = {"answered", "replied", "bounced", "blocked", "capped", "promised", "unnamed"}


def canon(addr):
    """Canonical identity for one human mailbox.

    Gmail-style dots and +tags do not create separate mailboxes, and two pool rows for
    one person is the cheapest possible way to mail them twice. Compared on this, not on
    the raw string."""
    a = (addr or "").strip().lower()
    if "@" not in a:
        return a
    local, dom = a.split("@", 1)
    local = local.split("+", 1)[0].replace(".", "")
    return f"{local}@{dom}"


def _is_never_mail(addr):
    a = (addr or "").lower()
    if NEVER_MAIL.search(a):
        return True
    return bool(MACHINE_LOCAL.search(a.split("@", 1)[0]))


def is_non_human(addr):
    """True only for senders that cannot have written a reply by hand."""
    return bool(NON_HUMAN.search((addr or "").lower()))


def blocked_reason(addr):
    a = (addr or "").lower()
    if not a or "@" not in a:
        return "no address"
    dom = a.split("@", 1)[1]
    if a in R.ME:
        return "self"
    # ATS/job-board domains: exact domain or subdomain suffix only. Never substring:
    # substring matching once blocked all of unilever.com because of "lever.co".
    for b in R.BLOCK_DOMAIN_SUFFIX:
        if dom == b or dom.endswith("." + b):
            return f"ATS/machine domain ({dom})"
    # Banned orgs: match a whole domain LABEL containing the token, so "jio" catches
    # jio.com and jiostar.com but "ril" cannot catch a label like "april".
    labels = dom.split(".")
    for b in R.BLOCK_ORG_TOKENS:
        for lab in labels[:-1]:            # ignore the TLD
            if lab == b or (b in lab and len(b) >= 5):
                return f"blocked org ({dom})"
    if any(b in a for b in R.BLOCK_ADDR):
        return "do-not-mail address"
    if any(dom.startswith(p) or dom == p for p in R.PERSONAL_DOMAINS):
        return "personal mailbox"
    if dom in R.B_MAILBOX_HOLD:
        return f"Account B owns {dom}"
    if _is_never_mail(a):
        return "machine address"
    return None


def _name_contradicted(name, msg, lead):
    """True only when we have POSITIVE evidence the greeting belongs to someone else.

    The tie itself is already established upstream: greeting_name only reads messages
    whose To is this exact lead, so a name found there is a name we ourselves used for
    this address. Corporate local parts are frequently opaque (lavanyaibrahim@amazon.com is
    "Rhea Nair"), so demanding the name appear in the address would reject hundreds of
    perfectly good leads. The only check worth making is against the To display name,
    when Gmail gives us one."""
    n = (name or "").lower()
    disp = (msg.get("to_name") or "").strip().lower()
    if not n or not disp:
        return False
    tokens = [t for t in re.split(r"[\s.,'\-]+", disp) if t]
    if not tokens:
        return False
    if n in tokens or any(t.startswith(n) or n.startswith(t) for t in tokens):
        return False
    return True                      # a display name exists and shares nothing with it


def greeting_name(msgs, lead=None):
    """First plausible first-name used in our own outbound TO THIS LEAD in this thread.

    Reads only the first non-empty line of each message: a greeting always sits there,
    whereas scanning the whole body can lift a name out of a quoted or forwarded block."""
    for m in msgs:
        if m["sender"] not in R.ME:
            continue
        if lead and m.get("to") and m["to"] != lead:
            continue                      # not addressed to the person we are mailing
        for line in (m["text"] or "").splitlines():
            if not line.strip():
                continue
            g = GREET.match(line.strip())
            if g:
                cand = g.group(1).strip(".'-")
                if len(cand) >= 3 and cand.lower() not in BADNAME:
                    if lead is None or not _name_contradicted(cand, m, lead):
                        return cand
            break      # only the first non-empty line counts
    return None


def classify(lead, msgs, today_ms, sent_log_touches=0, min_quiet_days=21):
    """Return dict(verdict, state, reason, ...). verdict is ELIGIBLE | REPLY_DUE | SKIP.

    REPLY_DUE means a human answered and the ball is in our court: never auto-draft a
    follow-up there, surface it for a human-written reply instead."""
    out = {"lead": lead, "verdict": "SKIP", "state": "", "reason": "", "touches": 0,
           "first": None, "anchor": None, "last_out_ms": 0, "thread_id": None}
    if not msgs:
        out["reason"] = "empty thread"
        return out
    out["thread_id"] = msgs[0].get("threadId") or None

    # NOTE: blocked_reason() is deliberately NOT checked here. It used to be, and it
    # returned before the human-reply check below, so a reply from anyone at a banned or
    # closed org was never surfaced as REPLY_DUE. That is the documented failure this
    # project has already paid for once: the ban is a rule about not sending COLD
    # OUTREACH, and applying it to inbound once buried a Vedantu hiring contact asking
    # for a resume for five weeks, plus a contact writing from a hospital bed.
    # It became live again on 2026-09-10: adding "deloitte" to BLOCK_ORG_TOKENS correctly
    # stopped outbound, and as a side effect would have gagged replies from the 21
    # Deloitte leads still sitting in the pool.
    # The check now runs AFTER reply detection, so a human who writes still gets
    # surfaced, and only the outbound path is blocked.
    outs = [m for m in msgs if m["sender"] in R.ME]
    ins = [m for m in msgs if m["sender"] not in R.ME]

    # The org ban applies ONLY when no human has written to us. If someone from a banned
    # or closed org has replied, fall through so the reply check below can surface it as
    # REPLY_DUE; blocking here would bury it, which is the documented five-week Vedantu
    # failure. With no reply in play, block exactly as before.
    if not any(not is_non_human(m["sender"]) for m in ins):
        br = blocked_reason(lead)
        if br:
            out["reason"] = br
            out["state"] = "blocked"
            return out

    if not outs:
        out["reason"] = "no outbound from me"
        return out

    # Only outbound actually addressed to this lead counts as a touch or an anchor. A
    # forward of the same thread to a colleague is not a mail to them.
    mine_to_lead = [m for m in outs if (m.get("to") or "") == lead]
    if not mine_to_lead:
        out["reason"] = "no outbound addressed to this lead in the thread"
        out["state"] = "unnamed"
        return out
    out["touches"] = len(mine_to_lead) + sent_log_touches
    out["last_out_ms"] = mine_to_lead[-1]["date_ms"]
    out["anchor"] = mine_to_lead[-1]

    # A human reply outranks everything. Checked BEFORE the bounce scan so that ordinary
    # prose ("hiring is blocked until Q3") can never be read as an undeliverable address.
    humans = [m for m in ins if not is_non_human(m["sender"])]
    if humans:
        answered = msgs[-1]["sender"] in R.ME
        out["verdict"] = "SKIP" if answered else "REPLY_DUE"
        out["state"] = "answered" if answered else "replied"
        out["reason"] = f"human reply from {humans[-1]['sender']}" + ("; already answered" if answered else "")
        out["first"] = greeting_name(msgs, lead)
        # Surfaced even for a banned or closed org: they wrote to us, and a person who
        # asks a question is owed an answer regardless of whether we would cold-mail
        # their company. REPLY_DUE is never auto-drafted anyway, so this only decides
        # whether a human gets to see it.
        if blocked_reason(lead):
            out["reason"] += f"; NOTE org is blocked ({blocked_reason(lead)}), reply by hand only"
        return out

    # Bounce evidence, evaluated ONLY on machine-sent mail.
    for m in ins:
        if not is_non_human(m["sender"]):
            continue
        # search every MIME part of the report, not just the chosen body
        blob = " ".join([m.get("subject", "") or "", m.get("all_text") or "", m["text"] or ""])
        if BOUNCE.search(blob):
            out["verdict"] = "SKIP"
            out["state"] = "bounced"
            out["reason"] = "bounced address"
            return out

    for m in mine_to_lead:
        if PROMISE.search(m["text"] or ""):
            out["reason"] = "silence promised in an earlier mail"
            out["state"] = "promised"
            return out

    if out["touches"] >= R.TOUCH_CAP:
        out["reason"] = f"touch cap reached ({out['touches']}/{R.TOUCH_CAP})"
        out["state"] = "capped"
        return out

    quiet_days = (today_ms - out["last_out_ms"]) / 86400000.0
    if quiet_days < min_quiet_days:
        out["reason"] = f"last touch only {quiet_days:.0f}d ago (min {min_quiet_days}d)"
        out["state"] = "too_recent"
        return out

    name = greeting_name(msgs, lead)
    if not name:
        out["reason"] = "no first name verifiably tied to this recipient"
        out["state"] = "unnamed"
        return out

    out["first"] = name
    out["verdict"] = "ELIGIBLE"
    out["state"] = "eligible"
    out["reason"] = f"{out['touches']} touches, quiet {quiet_days:.0f}d"
    out["quiet_days"] = round(quiet_days)
    return out
