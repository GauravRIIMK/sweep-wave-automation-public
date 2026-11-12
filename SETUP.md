# Setup (about 5 minutes, done once)

The automation needs its own authority to your Gmail, because a GitHub runner cannot use
the desktop app's connector. Nothing here shares your password: OAuth issues a token that
can read mail and create drafts, and you can revoke it at any time.

## 1. Create the Google OAuth client

1. Open <https://console.cloud.google.com/projectcreate> and make a project (any name).
2. Go to **APIs and Services > Library**, search **Gmail API**, click **Enable**.
3. Go to **APIs and Services > OAuth consent screen**. Choose **External**, fill the
   required name and your own email, and **Save**. Under **Audience**, add
   `rathore.gaurav1007@gmail.com` as a **Test user**. (Test mode is fine and avoids review.)
4. Go to **Credentials > Create credentials > OAuth client ID**, application type
   **Desktop app**, create, then **Download JSON**.

## 1a. Branding fields required before publishing

Google will not switch the app to production until **Branding** is complete. Use:

| Field | Value |
|---|---|
| App name | `Outreach Bot` |
| User support email | `rathore.gaurav1007@gmail.com` |
| Application home page | `https://gauravriimk.github.io/outreach-bot-info/` |
| Privacy policy link | `https://gauravriimk.github.io/outreach-bot-info/privacy.html` |
| Authorized domain | `gauravriimk.github.io` |
| Developer contact email | `rathore.gaurav1007@gmail.com` |

Those two pages are served from the public repo `GauravRIIMK/outreach-bot-info`.

**Authorized domain must be `gauravriimk.github.io`, NOT `github.io`.** `github.io` is on
the Public Suffix List, so Google rejects it with "must be a top private domain": it is a
suffix many people publish under, not a domain anyone owns. The owned name is the label
below it. Terms of Service may be left blank.

## 1b. PUBLISH the app (do NOT skip: this is what keeps it working)

Still in **Google Auth Platform > Audience**, find **Publishing status**.

- If it says **Testing**, click **PUBLISH APP** and confirm.
- Leave User type as **External**. You do NOT need to submit for verification.

Why this matters more than it looks: while an external app sits in **Testing**, Google
expires every refresh token it issues after **7 DAYS**. The automation would authenticate
once, run for a week, then fail every morning until someone re-minted a token by hand.
Publishing removes that expiry. The token then lasts until it is revoked, unused for six
months, or the account password changes.

The trade-off is cosmetic and one-time: because the app is unverified, the consent screen
shows "Google hasn't verified this app". Click **Advanced**, then **Go to (your app name)
(unsafe)**. That warning appears only while you are granting consent, never afterwards.
An unverified production app is capped at 100 users, which is irrelevant here: the only
user is you.

If you would rather stay in Testing, you must instead add
`rathore.gaurav1007@gmail.com` under **Test users**, and accept re-minting the token
every 7 days.

## 2. One command sets all four secrets

```powershell
cd "C:\Users\Gaurav Rathore\Downloads\sweep-wave-automation"
```

```powershell
C:\Python314\python.exe tools\bootstrap_secrets.py
```

It finds `client_secret*.json` in Downloads, opens the browser for consent, and writes all
four secrets to the repo. Every value is piped to `gh secret set` over stdin, so none is
ever printed, typed, placed on a command line, or written to shell history. The Anthropic
key is entered at a hidden prompt.

Delete `client_secret*.json` afterwards; nothing else needs it.

## 2b. Manual alternative: mint the refresh token yourself


Windows PowerShell 5.1 does NOT support `&&`. Run these as separate lines:

```powershell
pip install google-auth-oauthlib
```

```powershell
cd "C:\Users\Gaurav Rathore\Downloads\sweep-wave-automation"
```

Then run the script with the REAL path to the file you downloaded in step 1. If it is
in your Downloads folder with its default name, this finds it automatically:

```powershell
$cs = (Get-ChildItem "$HOME\Downloads\client_secret*.json" | Select-Object -First 1).FullName
C:\Python314\python.exe tools\get_refresh_token.py $cs
```

A browser opens. **Sign in as rathore.gaurav1007@gmail.com** and approve. Google will warn
that the app is unverified: choose **Advanced > Go to (your app)**. The script prints three
values.

## 3. Add the secrets

Repo > **Settings > Secrets and variables > Actions > New repository secret**:

| Secret | Value |
|---|---|
| `GMAIL_CLIENT_ID` | from the script output |
| `GMAIL_CLIENT_SECRET` | from the script output |
| `GMAIL_REFRESH_TOKEN` | from the script output |
| `ANTHROPIC_API_KEY` | from <https://console.anthropic.com/settings/keys> |

Then delete the terminal output and the downloaded JSON file.

## 4. Prove it works before it mails anyone

Actions tab > **Daily outreach batch** > **Run workflow** > tick **dry_run** > Run.

The job summary will show what it would have created, with the research confidence per
lead, and the reply-due list. Read a few crafted mails in the uploaded artifact. When you
are happy, run it again with `dry_run` unchecked, or just let the 11:15 AM IST schedule
take over.

## 5. Retire the local scheduled task

The desktop task `sweep-wave-daily-batch` and this workflow must never both be live, or
leads get mailed twice. Once a real (non-dry-run) GitHub run succeeds, `state/meta.json`
gets `"gh_live": true`, and the local task checks that and stands itself down. You can also
just delete the local task from the Scheduled panel in the desktop app.

## Cost

Roughly 2 model calls per lead (research with web search, then crafting). At about
150 leads a day this is a few dollars a day on the Anthropic key, and GitHub Actions
minutes are free for private repos within the monthly allowance.

## Revoking access

<https://myaccount.google.com/permissions> > your app > **Remove access**. The workflow
then fails its identity gate and mails nobody.
