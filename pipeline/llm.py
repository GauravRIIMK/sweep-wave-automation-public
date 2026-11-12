# -*- coding: utf-8 -*-
"""Anthropic Messages API client. One secret (ANTHROPIC_API_KEY) powers both the
grounded LinkedIn research (server-side web search) and the per-lead crafting."""
import json, os, time
import requests

URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("CRAFT_MODEL", "claude-sonnet-5")
RESEARCH_MODEL = os.environ.get("RESEARCH_MODEL", "claude-sonnet-5")


def _headers():
    h = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    # An organisation-level API key is not tied to a workspace, and the API then rejects
    # every request unless the workspace is named explicitly. Setting a workspace-scoped
    # key is the cleaner fix; this header exists so an existing org key also works.
    ws = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    if ws:
        h["anthropic-workspace-id"] = ws
    return h


# WHAT THIS RUN COSTS, measured rather than guessed.
#
# Every response carries a usage block and this client threw it away, so the only answer
# to "what is burning the credits" was arithmetic on assumptions. Totals are accumulated
# per process and written into the run report, because the two call sites have wildly
# different costs and nobody could see which.
#
# List prices, claude-sonnet-5: $2.00 per MTok in, $10.00 per MTok out.
# Server-side web search is billed SEPARATELY at $10 per 1,000 searches, AND the results
# it injects are charged again as input tokens on every following turn of that request.
# That second part is the one that surprises people.
PRICE_IN_PER_MTOK = float(os.environ.get("PRICE_IN_PER_MTOK", "2.00"))
PRICE_OUT_PER_MTOK = float(os.environ.get("PRICE_OUT_PER_MTOK", "10.00"))
PRICE_PER_SEARCH = float(os.environ.get("PRICE_PER_SEARCH", "0.01"))

USAGE = {}


def _record_usage(model, u):
    # Split by whether the call used a server tool: research is the only caller that
    # passes web search, so this separates the two cost centres without threading a
    # label through every call.
    tag = "research" if u.get("server_tool_use") else "craft"
    d = USAGE.setdefault(tag, {"calls": 0, "in": 0, "out": 0, "cache_read": 0, "searches": 0})
    d["calls"] += 1
    d["in"] += int(u.get("input_tokens") or 0)
    d["out"] += int(u.get("output_tokens") or 0)
    d["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
    d["searches"] += int((u.get("server_tool_use") or {}).get("web_search_requests") or 0)


def usage_summary():
    """Per call-site totals plus a list-price estimate. Cheap, and it ends the guessing."""
    out, total = {}, 0.0
    for tag, d in USAGE.items():
        cost = (d["in"] / 1e6 * PRICE_IN_PER_MTOK
                + d["out"] / 1e6 * PRICE_OUT_PER_MTOK
                + d["searches"] * PRICE_PER_SEARCH)
        total += cost
        out[tag] = dict(d, est_usd=round(cost, 4))
    out["TOTAL_est_usd"] = round(total, 2)
    return out


def call(system, user, model=MODEL, max_tokens=1600, tools=None, retries=3):
    payload = {"model": model, "max_tokens": max_tokens,
               "system": system, "messages": [{"role": "user", "content": user}]}
    if tools:
        payload["tools"] = tools
    last = None
    for attempt in range(retries):
        r = requests.post(URL, headers=_headers(), json=payload, timeout=180)
        if r.status_code in (429, 500, 502, 503, 529):
            last = f"{r.status_code}: {r.text[:200]}"
            time.sleep(2 ** attempt * 5)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"anthropic {r.status_code}: {r.text[:500]}")
        data = r.json()
        _record_usage(model, data.get("usage") or {})
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        return "\n".join(p for p in parts if p).strip()
    raise RuntimeError(f"anthropic unavailable after {retries} tries: {last}")


def call_json(system, user, model=MODEL, max_tokens=1600, tools=None):
    """Ask for JSON and parse defensively (models sometimes wrap it in prose/fences)."""
    txt = call(system + "\n\nReply with ONE JSON object and nothing else.",
               user, model=model, max_tokens=max_tokens, tools=tools)
    s, e = txt.find("{"), txt.rfind("}")
    if s < 0 or e <= s:
        raise ValueError(f"no JSON in model output: {txt[:300]}")
    return json.loads(txt[s:e + 1])


WEB_SEARCH_MAX_USES = int(os.environ.get("WEB_SEARCH_MAX_USES", "6"))
WEB_SEARCH_TOOL = [{"type": "web_search_20250305", "name": "web_search",
                    "max_uses": WEB_SEARCH_MAX_USES}]
