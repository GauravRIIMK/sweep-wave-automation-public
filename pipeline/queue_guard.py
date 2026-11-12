# -*- coding: utf-8 -*-
"""Protect the outbound queue from going stale.

A created draft is not sent immediately: this mailbox dispatches in a single daily burst
(observed 04:03-04:35 UTC). With a deep queue a draft can wait days. If the recipient
replies during that gap, the follow-up goes out ON TOP of their answer, which is the most
embarrassing failure this system can produce: it proves nobody read what they wrote.

The rule for trashing is deliberately narrow:

    trash a queued draft only when a HUMAN reply arrived in that thread AFTER the draft
    was created, and only for an address this pipeline tracks.

The "after the draft was created" comparison is what makes this safe. A hand-written
reply also sits in a thread that ends with an inbound message, but it answers a message
OLDER than itself, so it is never touched. A stale follow-up is the opposite: the inbound
is NEWER than the draft. Nothing else distinguishes the two reliably.
"""
from . import classify
from . import registries as R


def sweep(g, tracked, dry_run=False, limit=400, log=print):
    """Return [{email, thread_id, replied_by, replied_at, draft_age_h, trashed}].

    `tracked` is a set of canonical addresses this pipeline is responsible for. Drafts to
    anyone else are never touched, however old they look.
    """
    rescued, examined, errors = [], 0, []
    try:
        drafts = g.list_drafts()
    except Exception as e:
        return rescued, [f"list_drafts failed: {e}"]

    for d in drafts[:limit]:
        try:
            dd = g.draft_detail(d["draft_id"])
        except Exception as e:
            errors.append(f"draft_detail {d['draft_id']}: {e}")
            continue
        if not dd or not dd.get("to") or not dd.get("threadId"):
            continue
        lead = dd["to"].lower()
        if classify.canon(lead) not in tracked:
            continue                      # not ours: never touch it
        examined += 1
        try:
            msgs = g.thread_full(dd["threadId"])
        except Exception as e:
            errors.append(f"thread {dd['threadId']}: {e}")
            continue
        if not msgs:
            continue

        draft_ms = dd.get("internalDate") or 0
        # Any human inbound strictly newer than the draft means they answered while our
        # follow-up was still sitting in the queue.
        later_human = [m for m in msgs
                       if m["sender"] not in R.ME
                       and not classify.is_non_human(m["sender"])
                       and m["date_ms"] > draft_ms]
        if not later_human:
            continue

        newest = max(later_human, key=lambda m: m["date_ms"])
        row = {"email": lead, "thread_id": dd["threadId"],
               "replied_by": newest["sender"],
               "gap_hours": round((newest["date_ms"] - draft_ms) / 3600000.0, 1),
               "trashed": False}
        if not dry_run:
            try:
                g.trash_message(dd["msg_id"])
                row["trashed"] = True
            except Exception as e:
                errors.append(f"trash {dd['msg_id']}: {e}")
        rescued.append(row)
        log(f"  RESCUED {lead}: {newest['sender']} replied "
            f"{row['gap_hours']}h after the draft was queued"
            + ("" if row["trashed"] else "  (DRY RUN, not trashed)"))

    log(f"queue guard: examined {examined} tracked drafts, rescued {len(rescued)}")
    return rescued, errors
