# -*- coding: utf-8 -*-
"""Hard registries and doctrine constants. These outrank every other signal.
Editing this file is the ONLY supported way to change who may be mailed."""

ME = {"rathore.gaurav1007@gmail.com", "gaurav.rathore11077@gmail.com"}

# Banned organisations. Matched against each LABEL of the domain, so "jio" blocks
# jio.com and jiostar.com without blocking a label that merely contains the letters.
BLOCK_ORG_TOKENS = [
    "vedantu", "jio", "reliance", "ril", "revolut", "leverageedu", "hotstar", "viacom",
    "nw18", "network18", "jiostar", "disneystar", "puma", "eruditus", "emeritus",
    "pocketfm", "sarvam", "glance", "inmobi", "gobblecube", "shopdeck", "blitzscale",
    "noon", "scalerailabs", "meesho", "1mg", "uber",
    # Added 2026-09-10. Deloitte closed on 2026-08-27 when BOTH offers fell through at
    # negotiation, and the rejected-orgs registry was updated the same day, but the
    # decision never reached this file. A live dispatch dry run on 2026-09-10 reported
    # it "would send" to schoudhuri@, sbagla@ and zubin@deloitte.com, which is how
    # the gap surfaced. Unit tests could not have found this; only running the real
    # pipeline against the real mailbox did.
    "deloitte",
]
# ATS, job-board and machine domains. Matched as an EXACT domain or a subdomain suffix.
# Substring matching here caused a real incident: "lever.co" silently blocked every
# address at unilever.com, removing 21 legitimate leads at a target company.
BLOCK_DOMAIN_SUFFIX = [
    "amazon.jobs", "binary.so", "ashbyhq.com", "greenhouse.io", "myworkday.com",
    "icims.com", "lever.co", "jobvite.com", "smartrecruiters.com", "workday.com",
    "jobalerts-ta.pwc.com", "bigitjobs.com", "hireveda.com", "bigsocialrecruiting.com",
]
# Never mail these exact addresses (substring match on the full address).
BLOCK_ADDR = [
    "kritihegde@", "ishani.divya@hp.com", "omkarmadan@amazon.com", "jatingill@amazon.com",
    "charu@swiggy.in", "s.gill@salesforce.com", "tanu.srivastava@", "azeem.khwaja@",
    "rashi.agarwal@",
]
# Personal mailboxes of corporate leads: never a substitute for the work address.
PERSONAL_DOMAINS = ("gmail.com", "hotmail.com", "yahoo.", "outlook.com", "rediffmail.com",
                    "protonmail.com", "icloud.com")

# Cross-mailbox hold: Account B owns these conversations; this runner cannot see B's touches.
B_MAILBOX_HOLD = ("salesforce.com", "airtel.com", "airtelbank.com")

# Hard cap on total outbound per human, across every thread and both mailboxes.
TOUCH_CAP = 6

# Machine senders: never treated as a human reply.
# Two DIFFERENT questions, deliberately two registries.
#
# NEVER_MAIL_PAT: "may we send TO this address?" Shared inboxes like careers@ belong here:
# cold-mailing them is pointless.
# NON_HUMAN_PAT: "was this inbound written by a machine?" careers@ must NOT be here. Real
# recruiters answer from shared inboxes, and treating that as noise means the pipeline
# writes again as though nobody ever replied.
NEVER_MAIL_PAT = (
    r"(no-?reply|noreply|donotreply|do-not-reply|mailer-daemon|postmaster|careers@|jobs@|"
    r"recruiting@|notification|messages@|invitations@|updates?@|newsletter|digest|"
    r"alert|mailer@|bounce|rs-manavjaiswal|googlegroups|calendar-notification|"
    r"auto-?confirm|automated|robot|jobs-listing|talentacquisition@)"
)
NON_HUMAN_PAT = (
    r"(no-?reply|noreply|donotreply|do-not-reply|mailer-daemon|postmaster|"
    r"notification|newsletter|digest|calendar-notification|invitations@|"
    r"googlegroups|auto-?confirm|automated|robot|mailer@)"
)
MACHINE_PAT = NEVER_MAIL_PAT          # backwards-compatible alias
# A local part that is a long hex/uuid blob is machine-generated, never a person.
MACHINE_LOCALPART = r"^[0-9a-f]{16,}$|^[0-9a-f]{8}-[0-9a-f]{4}-"
# Bounce wording. Every alternative must be machine-delivery evidence, never ordinary
# prose. Bare "blocked", "does not exist" and "mailbox is full" were removed after they
# misread real human replies ("Hiring is blocked until Q3", "That team does not exist
# anymore") as undeliverable addresses, permanently killing live leads.
# This is additionally only ever evaluated on mail FROM a machine sender.
BOUNCE_PAT = (
    r"(address not found|couldn'?t be found|recipient unknown|user unknown|"
    r"address does not exist|account does not exist|no such user|"
    r"delivery status notification \(failure\)|undeliverable|unable to receive mail|"
    r"mail delivery failed|returning message to sender|undelivered mail returned|"
    r"delivery has failed to these recipients|recipient address rejected|"
    r"mailbox full|quota exceeded|message blocked|blocked for spam|"
    r"550 5\.[0-9]|552 5\.2\.2|5\.1\.1|5\.7\.1)"
)
# A prior promise of silence permanently closes the thread.
PROMISE_PAT = (
    # Built from the real sent corpus, not from intuition: a scan of 1,016 closing-like
    # fragments in outbound mail showed 458 that the original pattern missed, including
    # "and I will stop here", which let a live draft go out to someone already told we
    # would stop. The asymmetry is decisive: a false positive costs one opportunity, a
    # false negative breaks a promise made in writing. Bias hard toward catching.
    r"("
    r"final note|last note|last one\b|last time I will write|last email you will get|"
    r"(second|third|fourth|fifth|sixth|seventh) and (final|last)|"
    r"letting it rest|close the loop|leave (it|this) (entirely|alone|after|there|here)|"
    r"leave this requisition alone|stop adding to your inbox|not add to your inbox|"
    r"will not write again|will make this the last|last (note|one|email|mail) I will send|"
    r"I will not repeat|will not be writing again|"
    # --- added from corpus evidence ---
    r"I will stop|where I stop|I will leave it|I stop (here|there)|"
    r"is the (last|final)\b|the final (note|mail|email|follow.?up|one|word)|"
    r"(brief and|one last|a) final follow.?up|final follow.?up|"
    r"my last\b|one is my last|is my last\b|my last (note|mail|email|to)\b|this is my last\b|"
    r"no further (notes|mails|emails|contact)|nothing further from me|"
    r"will not trouble you|will not bother you|not write to you again|"
    r"take that as a no|closes it cleanly|and it is the last|"
    # --- added 2026-10-13 after four promise breaches were QUEUED on 10-01 ---
    # Kyndryl "it will be the last", Maersk "I shall make this the closing one",
    # MMC "will make this third one the last", Edelweiss "leave the thread alone".
    # All four are one word away from a form already listed, so these alternatives
    # generalise the SHAPE instead of adding four more literals.
    r"(will|shall|would|am going to) be (the|my) (last|final)|"
    r"(will|shall|would) (make|keep) this (the|my) (last|final|closing)|"
    r"(the|my) closing (one|note|mail|email|word)|make this the closing|"
    r"(this|that|third|fourth|fifth|second) one the (last|final)|"
    r"leave (the thread|the matter|this thread|it) alone|"
    r"(shall|will) (not|never) write (to you )?again|"
    r"(shall|will) leave (it|this|the thread) (alone|there|here|be)|"
    r"once more and then|and then leave|after this (note|one|mail|email) I"
    r")"
)

# Banned phrasings in any crafted mail (tone doctrine).
BANNED_PHRASES = [
    "following up", "follow up", "follow-up", "reminder", "resurface", "resurfacing",
    "circling back", "circle back", "just checking", "checking in", "bumping",
    "final note", "last note", "as promised", "per my last", "gentle nudge", "nudge",
    "i know you are busy", "sorry to bother", "apologies for the", "quick question",
    "15 minutes", "fifteen minutes", "calendly", "book a slot", "hop on a call",
    "let me know a good time", "looking forward to hearing", "at your earliest convenience",
]
# A former employer's INTERNAL figures must never reach outreach, and least of all a
# competitor. Blinkit and Blinkit Bistro compete directly with Swiggy Instamart, Zepto,
# Flipkart Minutes and JioMart, all of which are live targets. These are banned outright,
# not merely ungrounded: the story is usable, the number never is. Spelled-out forms are
# listed because a digit check alone does not catch "sixty to seventy percent".
BANNED_FIGURES = [
    ("60 to 70", "cannibalization displacement, Blinkit internal"),
    ("60-70", "cannibalization displacement, Blinkit internal"),
    ("60 - 70", "cannibalization displacement, Blinkit internal"),
    ("sixty to seventy", "cannibalization displacement, Blinkit internal"),
    ("5.7l", "Bistro projected monthly margin saving, internal"),
    ("5.7 l", "Bistro projected monthly margin saving, internal"),
    ("5.7 lakh", "Bistro projected monthly margin saving, internal"),
    ("thirteen margin interventions", "Bistro internal intervention count"),
    ("121,582", "Bistro order volume, internal"),
    ("121582", "Bistro order volume, internal"),
    ("6,095 ratings", "Bistro ratings volume, internal"),
    ("14 ncr pods", "Bistro pod count, internal"),
]

# Factual guards: the left string must never appear; the right is the truth.
FACT_GUARDS = [
    ("15 cr", "Rs 1.5 Cr"), ("15 crore", "Rs 1.5 Cr"), ("₹15", "Rs 1.5 Cr"),
    ("/in/gauravrathore", "/in/gaurav1-grow-learn-together"),
]
REQUIRED_SIGNATURE = "https://www.linkedin.com/in/gaurav1-grow-learn-together"
PHONE = "+91 7888939968"

# Canonical, verified candidate facts. The crafting model may use ONLY these.
CANDIDATE_FACTS = """
- Name: Gaurav Rathore. Based in Gurgaon, India.
- PAST, Apr 2025 to Jul 2026: Senior Manager, Operations at Blinkit Bistro. ALWAYS PAST
  TENSE, he has left. Managed station P&L, quality and delivery SLAs across about 50 cloud
  kitchens in 4 cities. Ran a set of margin interventions on those stations; the projected
  monthly rupee saving is INTERNAL and must never be stated. Weekly orders on his stations
  grew 12%. Complaint rate cut 94% across flagged pods, 40% of the quality gap closed in
  2.5 weeks. Ran a cannibalization study that changed a launch decision, because most of
  the orders a new store projected were being taken from a store that already existed; the
  displacement PERCENTAGE is INTERNAL and must never be stated.
- Prior: Blinkit. Led go to market for 38 DARK STORE launches (never "product launches",
  never at upGrad). Built the hyperlocal site selection framework on 500+ data points.
  Doubled daily active users across 5 cities. Cut marketing costs 40%. Cut churn 12% with
  behaviour-driven re-engagement. Built a real-time DAU and MTU dashboard.
- Prior: Growth Lead at upGrad. Built a referral engine from zero to Rs 1.5 Cr in four months
  (ALWAYS Rs 1.5 Cr, NEVER Rs 15 Cr). Two funnel rebuilds added about 6% incremental monthly
  revenue. 100+ career transitions.
- Prior: Great Learning, youngest department leader, international operations across 50+ nations.
- Prior: Thoughtworks, marketing program operations (Marketo and Demandbase, AI-assisted email
  workflows via Workato that cut manual effort about 85 percent). Describe as program operations.
- SQL and Python are hands-on: dark store site selection, churn cohorts, and upGrad funnel and
  segment analysis. Always name them together, never attribute one tool to one task.
- Education: MBA from IIM Kozhikode. Host of the CXO podcast "The Growth Code".
- Total experience: nearly 5 years (never "5+"). Lane, in order: Strategy, then Growth, then
  Marketing.
- Phone: +91 7888939968. LinkedIn: https://www.linkedin.com/in/gaurav1-grow-learn-together
- Target seniority for himself: Manager or Senior Manager. Never claim Director or above.
- NEVER claim: any impressions or reach figure (every such number on file is unconfirmed),
  availability or notice period, "5+ years", Rs 15 Cr, or that Bistro is current.
"""
