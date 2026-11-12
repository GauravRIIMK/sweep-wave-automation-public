# -*- coding: utf-8 -*-
"""Grounded per-lead research (LinkedIn-first), CONFIDENCE GATED.

Second-order rationale: a wrong-person match is strictly worse than no personalisation.
Namesakes are common (a.maheshwari@, r.karthik = Ujjwal Karthikeyan, two Nikhils at Scaler),
so research output is used ONLY when the model can tie the profile to BOTH the first name
we already greet them by AND the company domain we are mailing. Anything less returns
confidence "low" and the crafted mail falls back to thread-grounded content, which is
always safe because we wrote that thread ourselves.
"""
import json
from . import llm

SYSTEM = """You research one person so a job-seeker can write them a short, respectful,
accurate note. You search the public web (LinkedIn profile pages, company sites, conference
bios, press). You are rigorous about identity: many people share names.

Return JSON with exactly these keys:
  "confidence": "high" | "medium" | "low"
  "match_evidence": short string naming what tied the profile to this person, or why not
  "title": their current role title, verbatim, or null
  "team": the team/function/business unit they sit in, or null
  "org_context": one factual sentence about what their org or team is currently working on
                 that is relevant to growth, GTM, strategy or marketing, or null
  "hook": ONE specific, factual, non-flattering sentence a stranger could honestly reference
          about their work or team, or null
  "open_roles": array of 0-2 role titles currently open near them that suit a Manager or
                Senior Manager in growth/GTM/strategy/marketing, or []

RULES:
- "high" ONLY if the profile clearly matches the given first name AND the given company.
- "medium" if the company matches and the name is consistent but you cannot fully verify.
- "low" if you cannot find them, or if candidates are ambiguous, or the person appears to
  have LEFT that company. When "low", set title/team/org_context/hook to null.
- NEVER guess or infer a title you did not see. NEVER invent an open role.
- The hook must be a fact, not a compliment. No "impressive", "amazing", "loved your post".
- Keep every string under 220 characters."""


def research_lead(email, first_name, org, thread_subject=""):
    dom = email.split("@", 1)[1]
    user = f"""Person to research:
- First name we already address them by: {first_name}
- Email domain (their employer): {dom}
- Organisation (our label): {org}
- Subject line of our existing email thread with them: {thread_subject or "(none)"}

Find their LinkedIn profile and current role. Confirm they still work at {org} ({dom}).
Then return the JSON described in your instructions."""
    try:
        data = llm.call_json(SYSTEM, user, model=llm.RESEARCH_MODEL,
                             max_tokens=1400, tools=llm.WEB_SEARCH_TOOL)
    except Exception as e:
        return {"confidence": "low", "match_evidence": f"research failed: {e}",
                "title": None, "team": None, "org_context": None, "hook": None,
                "open_roles": [], "error": str(e)[:300]}

    conf = str(data.get("confidence", "low")).lower()
    if conf not in ("high", "medium", "low"):
        conf = "low"
    if conf == "low":
        for k in ("title", "team", "org_context", "hook"):
            data[k] = None
        data["open_roles"] = []
    data["confidence"] = conf
    if not isinstance(data.get("open_roles"), list):
        data["open_roles"] = []
    data["open_roles"] = [str(r)[:120] for r in data["open_roles"]][:2]
    return data
