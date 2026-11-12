# -*- coding: utf-8 -*-
"""Compile the local-only suppression sources into state/suppression.json.

    python -B -I tools/build_suppression.py [--check]

WHY THIS EXISTS
---------------
pipeline/resend_failed.py creates drafts in a mailbox that sends within hours, and
.github/workflows/resend.yml runs it on GitHub Actions. A gate is only a gate if its
source exists where the sending happens, and in CI almost none of these sources do:

    state/holds.json            TRACKED      visible in CI
    state/runs/*.json           TRACKED      visible in CI
    state/_promises.json        untracked    ABSENT in CI
    state/promise_rescan_*.json untracked    ABSENT in CI
    state/_recraft_skips.json   untracked    ABSENT in CI
    memory/do-not-mail-registry.md           outside the repo entirely

So the promise-of-silence and do-not-mail checks cannot read their own sources from a
checkout. This tool parses them HERE, where they live, and writes one tracked artefact the
gate can read anywhere. Re-run it whenever the registry or the promise ledgers change;
`--check` reports drift without writing, for use before a dispatch.

FOUR PARSE DEFECTS IN gate_review_holds.py THAT ARE FIXED HERE
--------------------------------------------------------------
That script is the prior art for this gate, and all four of these silently narrowed it:

1. `for a in pr.get('addresses', [])` then `str(a).lower()`. 57 of the 59 entries in
   _promises.json are OBJECTS ({address, date, thread_id, promise}), so stringifying them
   produced keys like "{'address': 'x@y.com', ...}" that can never equal an address.
   Fifty-seven of Gaurav's own promises of silence were invisible to it; only the 2 bare
   strings worked. Both shapes are read here.
2. The registry row regex requires the mailbox column to be `[AB]`, which misses every
   `both` row. All three org-scope rows are `both`, including
   `org:airtel.com + org:airtelbank.com | both | CAP3 ORG-WIDE AND PROMISE-OF-SILENCE`.
3. That same regex requires an `@`, so `org:<domain>` rows never matched at all and the
   org scope was absent entirely.
4. Addresses were compared raw-lowercased rather than through classify.canon, so a
   dot-variant or +tag of a suppressed address read as a different person.

A row can also name two orgs ("org:blitzscale.co + org:shopdeck.com"), and an address row
can name a second address in parentheses ("pooja.acharya@idfcfirst.bank.in
(+ @idfcfirstbank.com)"). Both forms are expanded.

Domains are matched by the CONSUMER as exact-or-suffix, never as a substring: a substring
ban on "lever.co" once blocked every unilever.com address.
"""
import argparse
import datetime
import glob
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import classify

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state")
MEM = (r"C:/Users/Gaurav Rathore/.claude/projects/"
       r"C--Users-Gaurav-Rathore-Downloads/memory")
DNM_MD = os.path.join(MEM, "do-not-mail-registry.md")
OUT = os.path.join(STATE, "suppression.json")

ADDR = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"

# Domains the root script's BANNED_DOMAINS bars but pipeline/registries.py does not, so
# classify.blocked_reason() lets them through. Kept here, as data, rather than edited into
# registries.py: that list is read by run_daily and dispatch, and widening a shared
# sending gate is a decision for a human, not a side effect of hardening one module.
#   disney.com         BLOCK_ORG_TOKENS has "disneystar", which does not match "disney"
#   jiomart.com        token "jio" only matches a whole label (the len(b) >= 5 guard)
#   idfcfirstbank.com  not in any list, though memory records IDFC as NO MAIL, org-wide
REGISTRY_GAP_DOMAINS = {
    "disney.com": "banned org (gap: registries.py has disneystar, not disney)",
    "jiomart.com": "banned org (gap: token 'jio' matches only a whole label)",
    "idfcfirstbank.com": "NO MAIL org-wide, user instruction 2026-10-14",
    "idfcfirst.bank.in": "NO MAIL org-wide, user instruction 2026-10-14",
}


def _canon(a):
    c = classify.canon((a or "").strip())
    return c if "@" in c and not c.startswith("@") else None


def parse_do_not_mail(path):
    """({canon addr: reason}, {domain: reason}) from the registry's REGISTRY rows.

    Format, per the file's own header:
        address-or-org:domain | mailbox(A/B/both) | reason | date-added
    """
    addrs, orgs = {}, {}
    if not os.path.exists(path):
        return addrs, orgs, False
    text = io.open(path, encoding="utf-8").read()
    for line in text.splitlines():
        if line.count("|") < 2:
            continue
        head, mailbox, rest = (line.split("|", 2) + ["", ""])[:3]
        head, mailbox = head.strip(), mailbox.strip().lower()
        if mailbox not in ("a", "b", "both"):       # not a registry row
            continue
        reason = rest.split("|")[0].strip()[:160]
        # org scope: one row may name several, "org:a.com + org:b.com"
        found_org = False
        for m in re.finditer(r"org:\s*([A-Za-z0-9.-]+\.[A-Za-z]{2,})", head):
            orgs[m.group(1).lower()] = f"do-not-mail org scope [{mailbox}]: {reason}"
            found_org = True
        if found_org:
            continue
        # address scope: the row may carry a second address in parentheses, and that
        # second form is sometimes a bare "@domain" continuation of the first local part
        local = head.split("@")[0].strip().lstrip("|").strip()
        for m in re.finditer(ADDR, head):
            c = _canon(m.group(0))
            if c:
                addrs[c] = f"do-not-mail registry [{mailbox}]: {reason}"
        for m in re.finditer(r"\(\s*\+?\s*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\s*\)", head):
            c = _canon(local + m.group(1))
            if c:
                addrs[c] = f"do-not-mail registry [{mailbox}]: {reason}"
    return addrs, orgs, True


def parse_promises():
    """{canon addr: evidence} for MY OWN promises of silence.

    Three sources, the same three gate_review_holds.py reads:
      _promises.json          addresses[], entries are objects OR bare strings
      promise_rescan_*.json   added_to_closed + already_held
      _recraft_skips.json     rows whose `why` mentions a promise, matched anywhere in
                              the string and not anchored: of the 14 live rows that
                              mention one, 8 do so mid-reason ("PRIOR PROMISE OF
                              SILENCE", "Separately trips the prior-promise-of-silence
                              gate", "Prior promise of silence, blocked"), so anchoring
                              would miss most of them. Deliberately tolerant: a lead
                              suppressed in error costs one repair, a lead mailed in
                              error costs a breach the recipient can see quoted.

    Deliberately NOT read: _promises_found / _unkept / _verified / _sendable / _final.
    Those record promises OTHER PEOPLE made to Gaurav ("I've forwarded your profile"),
    keyed on `promisor_email`. They are leads to chase, the opposite of a suppression, and
    _promises_sendable.json says so in its name.
    """
    out, seen = {}, []
    p = os.path.join(STATE, "_promises.json")
    if os.path.exists(p):
        seen.append("_promises.json")
        d = json.load(io.open(p, encoding="utf-8-sig"))
        for a in (d.get("addresses") or []):
            if isinstance(a, dict):
                c, q = _canon(a.get("address")), (a.get("promise") or "")[:140]
                why = f"promise ledger {a.get('date', '')}: {q}".strip()
            elif isinstance(a, str):
                c, why = _canon(a), "promise ledger"
            else:
                continue
            if c:
                out.setdefault(c, why)
    for f in sorted(glob.glob(os.path.join(STATE, "promise_rescan_*.json"))):
        seen.append(os.path.basename(f))
        d = json.load(io.open(f, encoding="utf-8-sig"))
        for a in (d.get("added_to_closed") or []) + (d.get("already_held") or []):
            c = _canon(a if isinstance(a, str) else (a or {}).get("address"))
            if c:
                out.setdefault(c, f"promise rescan ({os.path.basename(f)})")
    p = os.path.join(STATE, "_recraft_skips.json")
    if os.path.exists(p):
        seen.append("_recraft_skips.json")
        for sk in json.load(io.open(p, encoding="utf-8-sig")):
            why = (sk.get("why") or "")
            if "PROMISE" in why.upper():
                c = _canon(sk.get("to"))
                if c:
                    out.setdefault(c, why[:140])
    return out, seen


def build():
    addrs, orgs, dnm_ok = parse_do_not_mail(DNM_MD)
    promised, promise_sources = parse_promises()
    return {
        "generated": datetime.date.today().isoformat(),
        "built_by": "tools/build_suppression.py",
        "note": ("Machine copy of the suppression sources that are NOT in the repo. The "
                 "memory registry and the promise ledgers remain the source of truth; "
                 "re-run the builder after changing them. Addresses are classify.canon "
                 "form. Domains are matched exact-or-suffix by the consumer, never as a "
                 "substring."),
        "sources": {"do_not_mail_registry": DNM_MD if dnm_ok else "MISSING",
                    "promises": promise_sources},
        "do_not_mail": dict(sorted(addrs.items())),
        "do_not_mail_orgs": dict(sorted(orgs.items())),
        "promised_silence": dict(sorted(promised.items())),
        "registry_gap_domains": dict(sorted(REGISTRY_GAP_DOMAINS.items())),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="report drift against the committed file, write nothing")
    args = ap.parse_args()

    data = build()
    counts = {k: len(data[k]) for k in ("do_not_mail", "do_not_mail_orgs",
                                        "promised_silence", "registry_gap_domains")}
    print("do-not-mail registry : " + (data["sources"]["do_not_mail_registry"]))
    print("promise sources      : " + ", ".join(data["sources"]["promises"] or ["NONE"]))
    for k, n in counts.items():
        print(f"  {k:24s} {n}")

    if not data["do_not_mail"] and not data["promised_silence"]:
        print("REFUSING to write: both address sets are empty, which means the sources "
              "were not readable from here. Fix the paths rather than shipping an "
              "empty gate.")
        return 4

    if args.check:
        if not os.path.exists(OUT):
            print(f"DRIFT: {OUT} does not exist yet")
            return 1
        old = json.load(io.open(OUT, encoding="utf-8"))
        drift = {k: (len(old.get(k) or {}), len(data[k])) for k in counts
                 if (old.get(k) or {}) != data[k]}
        print("DRIFT: " + json.dumps(drift) if drift else "no drift")
        return 1 if drift else 0

    io.open(OUT, "w", encoding="utf-8", newline="\n").write(
        json.dumps(data, ensure_ascii=False, indent=1))
    print(f"\nwrote {OUT}")
    print("COMMIT IT: resend_failed reads this file, and GitHub Actions sees only "
          "tracked files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
