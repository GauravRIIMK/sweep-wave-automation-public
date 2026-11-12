# -*- coding: utf-8 -*-
"""One command to put all four secrets into the GitHub repo, without any value ever
being displayed, typed, logged, or pasted into a chat.

    python tools/bootstrap_secrets.py

How the values travel:
  client_id / client_secret : read straight out of client_secret.json on this machine
  refresh_token             : minted here in the browser, kept in memory only
  ANTHROPIC_API_KEY         : typed into a hidden prompt (not echoed, not in history)

Every value is handed to `gh secret set` over STDIN, so none of them appears on a command
line, in shell history, in a log file, or on screen. Nothing is written to disk.
"""
import getpass, glob, json, os, subprocess, sys

REPO = os.environ.get("REPO", "GauravRIIMK/sweep-wave-automation")
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
]


def fail(msg):
    print(f"\nERROR: {msg}")
    sys.exit(1)


# Shape checks. A silently mis-pasted secret is worse than a failed one: it is only
# discovered later, as an authentication error inside an unattended run.
SHAPE = {
    "GMAIL_CLIENT_ID":     dict(lo=40, hi=120, suffix=".apps.googleusercontent.com"),
    "GMAIL_CLIENT_SECRET": dict(lo=20, hi=60,  prefix="GOCSPX-"),
    "GMAIL_REFRESH_TOKEN": dict(lo=60, hi=400, prefix="1//"),
    "ANTHROPIC_API_KEY":   dict(lo=80, hi=200, prefix="sk-ant-"),
}


def check_shape(name, value):
    """Return an error string, or None when the value looks right."""
    r = SHAPE.get(name)
    if not r:
        return None
    n = len(value)
    if not (r["lo"] <= n <= r["hi"]):
        return (f"{name} is {n} characters; expected roughly {r['lo']} to {r['hi']}. "
                "The paste probably captured more (or less) than the value itself.")
    if r.get("prefix") and not value.startswith(r["prefix"]):
        return f"{name} should start with {r['prefix']!r}."
    if r.get("suffix") and not value.endswith(r["suffix"]):
        return f"{name} should end with {r['suffix']!r}."
    if len(value.split()) != 1:
        return f"{name} contains whitespace or a line break; it is not a clean single value."
    return None


def set_secret(name, value):
    """Pipe the value to `gh secret set` via stdin so it is never an argv element."""
    if not value or not value.strip():
        fail(f"{name} is empty; nothing was set.")
    value = value.strip()
    problem = check_shape(name, value)
    if problem:
        fail(problem + " Nothing was written.")
    p = subprocess.run(["gh", "secret", "set", name, "--repo", REPO],
                       input=value.strip().encode("utf-8"),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if p.returncode != 0:
        fail(f"gh secret set {name} failed: {p.stdout.decode('utf-8', 'replace')[:300]}")
    print(f"  set {name}  ({len(value.strip())} chars, value not shown)")


def find_client_json():
    if len(sys.argv) > 1:
        return sys.argv[1]
    pats = [os.path.expanduser("~/Downloads/client_secret*.json"),
            os.path.expanduser("~/Downloads/*apps.googleusercontent.com.json")]
    hits = sorted({h for pat in pats for h in glob.glob(pat)})
    if not hits:
        fail("no client_secret*.json found in Downloads; pass the path as an argument.")
    return hits[0]


def prompt_anthropic():
    """Ask for the API key, re-asking until it actually looks like one."""
    print("")
    print("Get a key at https://console.anthropic.com/settings/keys")
    print("Paste it below. It is not shown as you type and is not stored anywhere.")
    for attempt in range(3):
        key = getpass.getpass("ANTHROPIC_API_KEY: ").strip()
        problem = check_shape("ANTHROPIC_API_KEY", key)
        if not problem:
            set_secret("ANTHROPIC_API_KEY", key)
            return
        print(f"  rejected: {problem}")
        print("  Nothing was written. Copy ONLY the key, then try again.")
    fail("ANTHROPIC_API_KEY was not accepted after 3 attempts.")


def main():
    if "--only" in sys.argv:
        which = sys.argv[sys.argv.index("--only") + 1].upper()
        if which in ("ANTHROPIC", "ANTHROPIC_API_KEY"):
            prompt_anthropic()
            print("")
            print("secrets now on the repo:")
            subprocess.run(["gh", "secret", "list", "--repo", REPO])
            return
        fail(f"--only {which} is not supported (try: --only anthropic)")

    if subprocess.run(["gh", "auth", "status"], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode != 0:
        fail("GitHub CLI is not authenticated. Run: gh auth login")

    path = find_client_json()
    print(f"reading OAuth client from: {path}")
    try:
        blob = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        fail(f"could not read {path}: {e}")
    cfg = blob.get("installed") or blob.get("web") or {}
    cid, csec = cfg.get("client_id"), cfg.get("client_secret")
    if not cid or not csec:
        fail("client_id / client_secret missing; is this a Desktop app OAuth client?")

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        fail("missing dependency. Run: pip install google-auth-oauthlib")

    print("\nA browser window will open.")
    print("  1. Sign in as rathore.gaurav1007@gmail.com")
    print("  2. If you see 'Google hasn't verified this app':")
    print("     click Advanced, then 'Go to Outreach Bot (unsafe)'\n")
    flow = InstalledAppFlow.from_client_secrets_file(path, SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        fail("Google returned no refresh token. Revoke the app at "
             "https://myaccount.google.com/permissions and run this again.")

    print(f"\nwriting secrets to {REPO}:")
    set_secret("GMAIL_CLIENT_ID", cid)
    set_secret("GMAIL_CLIENT_SECRET", csec)
    set_secret("GMAIL_REFRESH_TOKEN", creds.refresh_token)

    print("\nLast one. Get a key at https://console.anthropic.com/settings/keys")
    print("Paste it below. It will NOT be shown as you type, and is not stored anywhere.")
    key = getpass.getpass("ANTHROPIC_API_KEY: ")
    set_secret("ANTHROPIC_API_KEY", key)

    print("\nsecrets now on the repo:")
    subprocess.run(["gh", "secret", "list", "--repo", REPO])
    print(f"\nDone. You can now delete {path} - it is no longer needed.")


if __name__ == "__main__":
    main()
