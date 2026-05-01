# Job Tracker Bot

Watches Gmail for job application emails, classifies them, and syncs everything
to a Notion database. Runs forever in the background via macOS launchd.

## What it does

- **Gmail polling** every 10 min for application/interview/offer/rejection emails
- **Heuristic classifier** extracts company, position, and status from subject + body
- **Local LLM fallback** (Ollama with Llama 3.2 3B) when heuristics can't find a position
- **Notion sync**: creates new rows for new applications, updates existing rows when
  status changes (e.g. Applied → Reject)
- **Re-application detection**: if a rejected role re-opens and you apply again, a
  fresh row is created instead of overwriting the rejection history
- **Idempotent dedup**: each Gmail message is processed exactly once via SQLite state

All processing is local. No data leaves your machine except the Notion API calls.

## Architecture

```
~/Desktop/02. Projects/job_tracking/
├── bot.py                          Main loop — polls Gmail every POLL_INTERVAL_SECONDS
├── classifier.py                   Heuristic extraction (regex patterns + ATS rules)
├── llm_extract.py                  Ollama client for position-extraction fallback
├── gmail_client.py                 Gmail OAuth + message fetching
├── notion_db.py                    Notion REST wrapper + fuzzy matcher + session cache
├── import_sheet.py                 One-time import of historical applications from xlsx
├── setup_notion.py                 One-time DB schema creation in Notion
├── requirements.txt
├── .env                            Secrets (gitignored)
├── .env.example                    Template
├── gmail_credentials.json          Google OAuth client (gitignored)
├── gmail_token.json                Cached refresh token (gitignored)
├── state.db                        SQLite: processed Gmail message IDs (gitignored)
├── bot.log                         Rotating log; up to 4 files × 5MB = 20MB max
└── com.danielhung.jobbot.plist     launchd LaunchAgent definition
```

The launchd plist is also installed at `~/Library/LaunchAgents/com.danielhung.jobbot.plist`.

## First-time setup (already done — kept here for re-install reference)

1. **Install dependencies**
   ```bash
   /opt/miniconda3/bin/python3 -m pip install -r requirements.txt
   ```

2. **Notion**
   - Get an integration token at https://www.notion.so/my-integrations
   - Create or pick a parent page, add the integration as a Connection
   - Fill `NOTION_TOKEN` and `NOTION_PARENT_PAGE_ID` in `.env`
   - Run `python setup_notion.py` — copies the printed DB ID into `.env`

3. **Gmail OAuth**
   - At [console.cloud.google.com](https://console.cloud.google.com): create project → enable Gmail API → OAuth consent screen (External, add yourself as Test User) → Credentials → OAuth client ID (Desktop app) → download JSON
   - Save downloaded JSON as `gmail_credentials.json` in project folder
   - First run of `bot.py` triggers consent screen in browser

4. **Import historical spreadsheet**
   ```bash
   python import_sheet.py /path/to/Job_Hunt_Tracking_2026.xlsx
   ```

5. **(Optional) Local LLM**
   ```bash
   brew install ollama
   ollama pull llama3.2:3b
   # In .env: USE_LLM_EXTRACTION=1
   ```

6. **Install as launchd LaunchAgent**
   ```bash
   cp com.danielhung.jobbot.plist ~/Library/LaunchAgents/
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.danielhung.jobbot.plist
   ```
   Then approve in **System Settings → General → Login Items & Extensions →
   Allow in the Background** (toggle `python3` ON).

## Day-to-day operations

### Check the bot is alive
```bash
ps aux | grep "job_tracking/bot.py" | grep -v grep
launchctl print gui/$(id -u)/com.danielhung.jobbot | grep "state ="
```
Both should show a running process / `state = running`.

### Watch live activity
```bash
tail -f "/Users/danielhung/Desktop/02. Projects/job_tracking/bot.log"
```

### Force a poll right now
```bash
launchctl kickstart -k gui/$(id -u)/com.danielhung.jobbot
```
This kills the sleeping process and restarts it; it'll immediately do a fresh pass.

### Deploy code changes
The running bot keeps the OLD code in memory. After editing any `.py`:
```bash
launchctl kickstart -k gui/$(id -u)/com.danielhung.jobbot
```

### Stop the bot
```bash
launchctl bootout gui/$(id -u)/com.danielhung.jobbot
```
To re-enable later:
```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.danielhung.jobbot.plist
```

### Run one pass manually (without launchd)
```bash
cd "/Users/danielhung/Desktop/02. Projects/job_tracking"
/opt/miniconda3/bin/python3 bot.py --once
```
Useful for debugging — output streams to the terminal AND bot.log.

## Configuration (`.env`)

| Key | Purpose |
|---|---|
| `NOTION_TOKEN` | Notion integration secret |
| `NOTION_PARENT_PAGE_ID` | Page where the DB lives (only used by setup_notion.py) |
| `NOTION_DATABASE_ID` | The Job Applications DB |
| `GMAIL_CREDENTIALS_PATH` | Path to OAuth client JSON |
| `GMAIL_TOKEN_PATH` | Path to cached refresh token |
| `POLL_INTERVAL_SECONDS` | How often to poll Gmail (default 600) |
| `INITIAL_LOOKBACK_DAYS` | First-run lookback window (default 180) |
| `USE_LLM_EXTRACTION` | `1` to enable Ollama fallback, `0` to disable |
| `OLLAMA_MODEL` | Model name (default `llama3.2:3b`) |

## Notion DB schema

| Field | Type | Set by |
|---|---|---|
| Position | title | Sheet import / email extraction (locked after create) |
| Company | rich_text | Sheet import / email extraction |
| Location | rich_text | Sheet import |
| Applied date | date | Sheet date / email date on first Applied (locked after) |
| Last update | date | Bumped on every email |
| Status | select | Applied / OA / Phone / Onsite / Offer / Reject / Ghosted / Withdrew / Needs Review |
| Source | select | LinkedIn / Greenhouse / Lever / Workday / Indeed / Direct / Referral / Other |
| Job link | url | Sheet hyperlink, then email URL |
| Last email subject | rich_text | Bot — also acts as a marker that this row was touched by the bot |
| Notes | rich_text | Manual |

## Maintenance

### Things that can break it (and how to fix)

**Gmail token revoked** (you changed Google password / hit 6-month inactivity / revoked the app at [myaccount.google.com/permissions](https://myaccount.google.com/permissions))
- Symptom: log shows `RefreshError`
- Fix: `rm gmail_token.json && python bot.py --once` to redo OAuth in a browser

**Notion integration revoked**
- Symptom: log shows `401` from Notion API
- Fix: re-add the integration as a Connection on your DB page

**macOS upgrade reset Login Items**
- Symptom: bot stops running, no errors logged because launchd never starts it
- Fix: re-toggle in **System Settings → General → Login Items & Extensions → Allow in the Background**

**Ollama down**
- Symptom: heuristics still work, no LLM fallback. Log says `Ollama not reachable` once at startup
- Fix: `brew services start ollama` (or open the Ollama menu bar app)

**Disk filling up with bot.log**
- Already mitigated: log rotates at 5 MB, keeps 3 backups (max ~20 MB total)
- Manual truncate if you want a clean slate: `: > bot.log`

### Once-a-week sanity check
```bash
tail -20 "/Users/danielhung/Desktop/02. Projects/job_tracking/bot.log"
```
Look for `Pass done:` lines and absence of `[ERROR]` / `Exception` messages.

## How matching works (when an email arrives)

1. **Classifier** extracts `(company, position, status)` from subject + body.
   If position can't be extracted and Ollama is enabled, asks the local LLM.

2. **Find a matching Notion row** by:
   - Session cache (just-created rows that Notion's eventual consistency hides)
   - Normalized company match (lowercased, suffixes stripped: `Stripe Inc.` == `Stripe`)
   - Position match: exact normalized (`SWE` == `Software Engineer`, `JR2011517 Foo` == `Foo`),
     then substring, then position-Unknown (matches any non-rejected row at the company)

3. **Re-application logic**: if all candidates for this company are `Reject`,
   the bot creates a fresh row ONLY IF the new email is dated AFTER the most recent
   rejection. Older confirmation emails arriving out of order get merged into the
   existing rejection.

4. **Update vs. create**: existing row → Status / Last update / Source / Last email
   subject / Job link are updated; Position and Applied date are preserved.

## Files NOT to commit

`.gitignore` already covers these, but for the record:
- `.env` (contains Notion token, Gmail paths)
- `gmail_credentials.json`, `gmail_token.json`
- `state.db`, `bot.log`, `bot.log.*`
- `__pycache__/`
