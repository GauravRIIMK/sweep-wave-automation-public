# -*- coding: utf-8 -*-
"""Thin Gmail REST API client (requests only). All calls act on users/me."""
import base64, email.utils, os, re, time

# The one CV that goes out with every mail. Repo-relative so a cloud run and a local run
# attach the identical file; the owner's canonical copy lives in OneDrive\Updated Resumes.
# Kept here as well as in dispatch.py because dispatch imports gmail_api, so the
# dependency cannot run the other way. dispatch.QUOTA_SIGNALS is the canonical list and a
# test asserts these two never drift.
QUOTA_SIGNALS = (
    "userratelimitexceeded", "ratelimitexceeded", "quotaexceeded",
    "too many concurrent requests", "limit for sending",
    "service invoked too many", "4.7.28",
)


def is_quota_error(exc):
    m = str(exc).lower()
    return any(s in m for s in QUOTA_SIGNALS)


CRLF = "\r\n"          # MIME line ending, spelled once
RESUME_NAME = "Gaurav_Rathore_Resume.pdf"
RESUME_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "assets", RESUME_NAME)
from html import unescape
import requests

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
EXPECTED_ACCOUNT = "rathore.gaurav1007@gmail.com"


class Gmail:
    def __init__(self, pace=None):
        self._token = None
        self._token_exp = 0
        # Minimum gap between calls. Gmail's per-user budget is generous per day but tight
        # per second, and a queue sweep issues hundreds of reads back to back.
        self._pace = float(os.environ.get("GMAIL_PACE_SECONDS", pace if pace is not None else 0.08))
        self._last_call = 0.0

    def _throttle(self):
        if self._pace <= 0:
            return
        gap = time.time() - self._last_call
        if gap < self._pace:
            time.sleep(self._pace - gap)
        self._last_call = time.time()

    def token(self):
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = requests.post("https://oauth2.googleapis.com/token", data={
            "client_id": os.environ["GMAIL_CLIENT_ID"],
            "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
            "refresh_token": os.environ["GMAIL_REFRESH_TOKEN"],
            "grant_type": "refresh_token",
        }, timeout=30)
        if r.status_code >= 400:
            body = r.text[:300]
            hint = ""
            if "invalid_grant" in body:
                hint = (" -- GMAIL_REFRESH_TOKEN is expired or revoked. The usual cause is "
                        "an OAuth app left in 'Testing' publishing status, where Google "
                        "expires refresh tokens after 7 DAYS. Publish the app to production "
                        "(see SETUP.md step 1b) and mint a new token.")
            raise RuntimeError(f"Gmail token refresh failed ({r.status_code}): {body}{hint}")
        d = r.json()
        self._token = d["access_token"]
        self._token_exp = time.time() + int(d.get("expires_in", 3600))
        return self._token

    def api(self, method, path, retries=5, **kw):
        # Only idempotent verbs may be retried. POST /drafts has no idempotency key, so a
        # retry after a lost or 5xx response (routine at Google scale) would create a
        # SECOND identical draft in the same thread, and the mailbox auto-sends both.
        if method.upper() not in ("GET", "HEAD"):
            retries = 1
        for attempt in range(retries):
            self._throttle()
            r = requests.request(method, BASE + path, timeout=60,
                                 headers={"Authorization": "Bearer " + self.token()}, **kw)
            # Gmail signals rate limiting with 403 (userRateLimitExceeded), NOT 429, so a
            # retry policy that only covers 429/5xx silently drops calls under load. That
            # showed up as 51 skipped drafts in a real queue sweep.
            rate_limited = (r.status_code == 403 and
                            any(t in r.text for t in ("rateLimitExceeded",
                                                      "userRateLimitExceeded",
                                                      "quotaExceeded",
                                                      "Too many concurrent requests")))
            if (r.status_code in (429, 500, 502, 503) or rate_limited) and attempt < retries - 1:
                wait = 2 ** attempt * 3
                if rate_limited:
                    self._pace = min(self._pace * 2 + 0.05, 1.0)   # back off future calls too
                time.sleep(wait)
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json() if r.text else {}
        raise RuntimeError("unreachable")

    # ---- identity gate ----
    def profile_email(self):
        return (self.api("GET", "/profile") or {}).get("emailAddress", "").lower()

    # ---- search / read ----
    def search_messages(self, q, cap=2000):
        """Return [{id, threadId}] for query q, fully paginated."""
        out, tok = [], None
        while True:
            params = {"q": q, "maxResults": 100}
            if tok:
                params["pageToken"] = tok
            d = self.api("GET", "/messages", params=params) or {}
            out.extend(d.get("messages", []))
            tok = d.get("nextPageToken")
            if not tok or len(out) >= cap:
                return out

    def message_meta(self, mid, headers=("From", "To", "Subject", "Message-ID", "Date")):
        d = self.api("GET", f"/messages/{mid}",
                     params={"format": "metadata", "metadataHeaders": list(headers)})
        if not d:
            return None
        h = {x["name"].lower(): x["value"] for x in d.get("payload", {}).get("headers", [])}
        return {"id": d["id"], "threadId": d.get("threadId"), "internalDate": int(d.get("internalDate", 0)),
                "labels": d.get("labelIds", []), "headers": h,
                "snippet": d.get("snippet", "")}

    @staticmethod
    def _walk_text(part, kind_filter=None):
        """Collect (kind, text) pairs. Never descends into message/rfc822: a bounce
        report embeds the ORIGINAL mail there, and treating that as the bounce's own body
        hides the delivery error behind our own words."""
        out = []
        mt = (part.get("mimeType") or "").lower()
        if mt.startswith("message/rfc822"):
            return out
        data = part.get("body", {}).get("data")
        if data and mt.startswith("text/plain"):
            out.append(("plain", base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace")))
        elif data and mt.startswith("text/html"):
            html = base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace")
            html = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", " ", html)
            txt = re.sub(r"<[^>]+>", " ", html)
            txt = unescape(txt)
            out.append(("html", re.sub(r"[ \t\xa0]+", " ", txt)))
        elif mt.startswith("message/delivery-status"):
            out.append(("dsn", "delivery status notification (failure)"))
        for p in part.get("parts", []) or []:
            out.extend(Gmail._walk_text(p))
        return out

    @staticmethod
    def _dequote(body):
        """Cut a message at the first quoted-reply boundary, whichever style it uses."""
        cuts = [
            r"\r?\n>",
            r"\r?\nOn .{5,120}?wrote:",
            r"\r?\n-{2,}\s*Original Message\s*-{2,}",
            r"\r?\n-{2,}\s*Forwarded message\s*-{2,}",
            r"\r?\n_{6,}",
            r"\r?\nFrom:\s",
        ]
        idx = len(body)
        for c in cuts:
            mm = re.search(c, body, re.I)
            if mm:
                idx = min(idx, mm.start())
        return body[:idx]

    def thread_full(self, tid):
        """Return list of {id, sender, to, date_ms, text, headers} oldest-first, or None."""
        d = self.api("GET", f"/threads/{tid}", params={"format": "full"})
        if not d:
            return None
        msgs = []
        for m in d.get("messages", []):
            h = {x["name"].lower(): x["value"] for x in m.get("payload", {}).get("headers", [])}
            sender = email.utils.parseaddr(h.get("from", ""))[1].lower()
            to_name, to_addr = email.utils.parseaddr(h.get("to", ""))
            parts = self._walk_text(m.get("payload", {}))
            # Prefer text/plain: the HTML rendition is systematically longer, so picking
            # the longest part pulled in CSS and un-prefixed quote chains.
            plain = [t for k, t in parts if k == "plain"]
            html = [t for k, t in parts if k == "html"]
            body = (max(plain, key=len) if plain else
                    (max(html, key=len) if html else m.get("snippet", "")))
            body = self._dequote(body)
            msgs.append({"id": m["id"], "sender": sender,
                         "to": to_addr.lower(), "to_name": to_name,
                         "date_ms": int(m.get("internalDate", 0)),
                         "subject": h.get("subject", ""),
                # Labels prove a trash actually landed. A trashed draft still answers
                # drafts.get, so "the record still exists" is NOT evidence the trash failed:
                # that mistake halted a healthy rebuild after 3 successful trashes.
                "labels": m.get("labelIds", []), "message_id_hdr": h.get("message-id", ""),
                         "text": body.strip()[:4000], "labels": m.get("labelIds", []),
                         "all_text": " ".join(t for _, t in parts)[:6000]})
        msgs.sort(key=lambda m: m["date_ms"])
        return msgs

    # ---- drafts ----
    def list_drafts(self):
        out, tok = [], None
        while True:
            params = {"maxResults": 100}
            if tok:
                params["pageToken"] = tok
            d = self.api("GET", "/drafts", params=params) or {}
            for dr in d.get("drafts", []):
                m = dr.get("message", {})
                out.append({"draft_id": dr["id"], "msg_id": m.get("id"), "threadId": m.get("threadId")})
            tok = d.get("nextPageToken")
            if not tok:
                return out

    def draft_detail(self, draft_id):
        d = self.api("GET", f"/drafts/{draft_id}", params={"format": "metadata"})
        if not d:
            return None
        m = d.get("message", {})
        h = {x["name"].lower(): x["value"] for x in m.get("payload", {}).get("headers", [])}
        return {"draft_id": d["id"], "msg_id": m.get("id"), "threadId": m.get("threadId"),
                "to": email.utils.parseaddr(h.get("to", ""))[1].lower(),
                # Carried so a rebuild can REFUSE a draft it would silently mangle: the
                # reply builder takes a single To and no Cc, so a draft with either extra
                # recipients or a Cc must be left alone rather than rewritten smaller.
                "to_raw": h.get("to", ""), "cc_raw": h.get("cc", ""),
                "subject": h.get("subject", ""),
                # sizeEstimate is how dispatch tells a draft that carries the resume from
                # one that does not. A body-only draft is ~2KB; the resume alone is 300KB.
                "sizeEstimate": int(m.get("sizeEstimate", 0) or self._msg_size(m.get("id"))),
                "internalDate": int(m.get("internalDate", 0))}

    def _msg_size(self, msg_id):
        """Bytes of the underlying message. drafts.get?format=metadata does NOT reliably
        carry sizeEstimate, and on 2026-09-17 that silently defeated the no-resume guard:
        the check read 0, `0 < 0` is false, and seven body-only drafts went out. One cheap
        minimal fetch is worth more than a guard that cannot see."""
        if not msg_id:
            return 0
        try:
            m = self.api("GET", f"/messages/{msg_id}", params={"format": "minimal"}) or {}
            return int(m.get("sizeEstimate", 0))
        except Exception:
            return 0

    def create_new_draft(self, to_addr, subject, body_text):
        """Open a NEW thread. Same multipart builder, same refusal without the resume.

        create_reply_draft posts with a threadId and therefore cannot start a conversation,
        which is why the pool has only ever held people who already had one: discover()
        reads `in:sent`, so a lead nobody has written to cannot enter the pipeline at all.
        That is fine for a follow-up engine and fatal once the backlog is spent, which is
        exactly where this account arrived on 2026-09-22, with three recyclable leads
        against a floor of 250.

        Deliberately NO In-Reply-To or References: inventing either would graft a first
        contact onto a conversation that never happened. Gmail assigns the thread, and the
        next daily run picks it up from `in:sent` through discover(), after which every
        follow-up runs through the existing in-thread path with nothing changed.
        """
        if not (subject or "").strip():
            raise ValueError("a new thread needs a subject")
        if not os.path.exists(RESUME_PATH):
            raise RuntimeError(
                f"resume missing at {RESUME_PATH}; refusing to build a draft without it")
        with open(RESUME_PATH, "rb") as f:
            pdf_b64 = base64.b64encode(f.read()).decode("ascii")
        pdf_b64 = CRLF.join(pdf_b64[i:i + 76] for i in range(0, len(pdf_b64), 76))
        bnd = "==GRathoreOutreachBoundary7f3a=="
        lines = [f"To: {to_addr}", f"Subject: {subject}", "MIME-Version: 1.0",
                 f'Content-Type: multipart/mixed; boundary="{bnd}"']
        raw = (CRLF.join(lines) + CRLF + CRLF
               + f"--{bnd}" + CRLF
               + "Content-Type: text/plain; charset=UTF-8" + CRLF
               + "Content-Transfer-Encoding: 7bit" + CRLF + CRLF
               + body_text + CRLF + CRLF
               + f"--{bnd}" + CRLF
               + f'Content-Type: application/pdf; name="{RESUME_NAME}"' + CRLF
               + f'Content-Disposition: attachment; filename="{RESUME_NAME}"' + CRLF
               + "Content-Transfer-Encoding: base64" + CRLF + CRLF
               + pdf_b64 + CRLF
               + f"--{bnd}--" + CRLF)
        enc = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
        return self.api("POST", "/drafts", json={"message": {"raw": enc}})

    def ever_mailed(self, addr):
        """True if ANY message to this address exists in Sent, at any date.

        The only honest test of "never mailed". A spreadsheet row, a note claiming 50
        leads, and the absence of a sent_log entry are all silent about what actually left
        this mailbox: most of this corpus was mailed by other tooling before the pipeline
        existed, and two cohorts a note called "pending" turned out to be 32 and 40 people
        already contacted.
        """
        try:
            return bool(self.search_messages(f"in:sent to:{addr}", cap=1))
        except Exception:
            return True     # fails CLOSED: unreadable is not evidence of never-mailed

    def attachments(self, msg_id):
        """Filenames of every attachment part on a message. [] when there are none.

        THE SIZE PROXY IS NOT PROOF. dispatch refused anything under MIN_SEND_BYTES on the
        theory that only the 300KB resume could push a mail past it. That theory is false
        in both directions and was caught on 2026-09-18:

          * a reply quoting a long inbound reached 141,762 bytes carrying NO attachment,
            which is three times the floor, and
          * the floor of 50,000 is itself an eighth of what the resume actually weighs on
            the wire (300,172 bytes of PDF, about 400,229 base64), so it was never testing
            the thing it claimed to test.

        The MIME part list is the only answer that cannot be faked by a verbose quote.
        `fields` keeps this cheap: Gmail returns the part headers alone, not the 400KB of
        base64 hanging off them, so this costs about as much as the metadata fetch it
        sits beside.
        """
        if not msg_id:
            return []
        try:
            m = self.api("GET", f"/messages/{msg_id}", params={
                "format": "full",
                "fields": "payload(filename,mimeType,parts(filename,mimeType,parts(filename,mimeType)))",
            }) or {}
        except Exception as e:
            # A QUOTA error here is not "this message has no attachment", it is "the
            # account is refusing to talk to us". Swallowing it returned [], which the
            # send guard reads as "no resume" and skips, so a rate limit on the READ path
            # would quietly relabel every remaining draft in the drain as unattached and
            # let the job finish green having sent nothing. Let it out so dispatch can
            # halt and arm the stand-down, exactly as it does for a refused send.
            if is_quota_error(e):
                raise
            # Anything else still fails CLOSED at the call site: an empty list reads as
            # "no resume", which refuses. An unreadable message is not evidence of a
            # good one.
            return []
        names = []

        def walk(part):
            fn = (part.get("filename") or "").strip()
            if fn:
                names.append(fn)
            for sub in (part.get("parts") or []):
                walk(sub)

        walk(m.get("payload") or {})
        return names

    def has_resume(self, msg_id):
        """True only when a part is literally the resume PDF, matched on filename."""
        want = RESUME_NAME.lower()
        return any(n.lower() == want for n in self.attachments(msg_id))

    def draft_body(self, draft_id):
        """Plain-text body of a draft, quoted history stripped. None if unreadable.

        draft_detail() deliberately fetches format=metadata (cheap, no body) because the
        queue guard only needs recipient and timestamp. The tone gate needs the actual
        prose, so this is a separate, heavier call made only where it is required.
        """
        d = self.api("GET", f"/drafts/{draft_id}", params={"format": "full"})
        if not d:
            return None
        m = d.get("message", {})
        parts = self._walk_text(m.get("payload", {}))
        plain = [t for k, t in parts if k == "plain"]
        html = [t for k, t in parts if k == "html"]
        body = (max(plain, key=len) if plain else
                (max(html, key=len) if html else m.get("snippet", "")))
        # _dequote strips the "On <date> X wrote:" chain. Without it the tone gate would
        # judge this mail on phrases quoted from every previous mail in the thread, and
        # older copy is full of release tails that are now banned.
        return self._dequote(body).strip()

    def create_reply_draft(self, to_addr, thread_id, anchor, body_text):
        """anchor = message dict with subject + message_id_hdr. Returns draft id.

        ALWAYS multipart, ALWAYS with the resume. Until 2026-09-17 this built a bare
        text/plain message, so every draft the pipeline created carried no attachment.
        That stayed invisible while the owner sent from the Gmail app, where he attaches
        the PDF by hand; the moment the cloud dispatcher sent 44 of them directly, 44
        people received a pitch with no resume behind it. A mail whose whole purpose is
        to put a CV in front of someone must not be constructible without one, so this
        raises rather than quietly degrading when the file is missing.
        """
        subj = anchor.get("subject", "") or ""
        if not subj.lower().startswith("re:"):
            subj = "Re: " + subj
        mid_hdr = anchor.get("message_id_hdr", "")
        if not os.path.exists(RESUME_PATH):
            raise RuntimeError(f"resume missing at {RESUME_PATH}; refusing to build a draft without it")
        with open(RESUME_PATH, "rb") as f:
            pdf_b64 = base64.b64encode(f.read()).decode("ascii")
        pdf_b64 = "\r\n".join(pdf_b64[i:i + 76] for i in range(0, len(pdf_b64), 76))
        # Boundary must not collide with the body or the base64 payload. A fixed literal
        # is safe here because both parts are 7-bit or base64 and neither can contain it.
        bnd = "==GRathoreOutreachBoundary7f3a=="
        lines = [f"To: {to_addr}", f"Subject: {subj}", "MIME-Version: 1.0",
                 f'Content-Type: multipart/mixed; boundary="{bnd}"']
        if mid_hdr:
            lines += [f"In-Reply-To: {mid_hdr}", f"References: {mid_hdr}"]
        raw = ("\r\n".join(lines) + "\r\n\r\n"
               + f"--{bnd}\r\n"
               + "Content-Type: text/plain; charset=UTF-8\r\n"
               + "Content-Transfer-Encoding: 7bit\r\n\r\n"
               + body_text + "\r\n\r\n"
               + f"--{bnd}\r\n"
               + f'Content-Type: application/pdf; name="{RESUME_NAME}"\r\n'
               + f'Content-Disposition: attachment; filename="{RESUME_NAME}"\r\n'
               + "Content-Transfer-Encoding: base64\r\n\r\n"
               + pdf_b64 + "\r\n"
               + f"--{bnd}--\r\n")
        enc = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
        d = self.api("POST", "/drafts", json={"message": {"threadId": thread_id, "raw": enc}})
        return d

    def send_draft(self, draft_id):
        """POST /drafts/send. Returns the sent message dict.

        NEVER retried. /drafts/send has no idempotency key, so a retry after a lost or
        5xx response would DELIVER THE MESSAGE TWICE to a real person. api() already
        forces retries=1 for non-GET; this docstring exists so nobody "fixes" that.

        A rate-limit (403 userRateLimitExceeded) therefore raises rather than retrying.
        That is deliberate: the caller must stand the whole run down, not skip ahead to
        the next draft. Skipping ahead against an account that is already refusing is
        exactly what wedged 84 messages in the Gmail Android Outbox on 2026-09-09.
        """
        return self.api("POST", "/drafts/send", json={"id": draft_id})

    def delete_draft(self, draft_id):
        """DELETE /drafts/{id}. The correct way to remove a DRAFT.

        trash_message() moves the underlying message to Trash, but the TRASH label does not
        appear on an immediate re-read, so verifying a trash is a race: a rebuild halted
        twice on three "failures" that were all successful trashes. Deleting the draft is
        unambiguous, and a following GET returning nothing is real evidence.
        """
        return self.api("DELETE", f"/drafts/{draft_id}")

    def trash_message(self, mid):
        return self.api("POST", f"/messages/{mid}/trash")
