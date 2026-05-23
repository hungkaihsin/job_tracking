# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A single-user, locally-running Python bot that watches Gmail for job-application emails, classifies them (heuristics first, LLM fallback), and upserts them into a Notion database. After each pass it mirrors Notion → a Google Sheet. State lives in `state.db` (SQLite). Managed by macOS launchd via `com.danielhung.jobbot.plist`.

No test suite. No build step. No linter configured. Production deployment is the launchd LaunchAgent already loaded on this machine.

## Day-to-day commands

```bash
# One pass + exit (use this for testing changes locally — streams to stdout AND bot.log)
/opt/miniconda3/bin/python3 bot.py --once

# Force the running launchd-managed bot to do a fresh pass NOW (kills the sleeping process)
launchctl kickstart -k gui/$(id -u)/com.danielhung.jobbot

# Watch live activity
tail -f bot.log

# Is it alive?
launchctl print gui/$(id -u)/com.danielhung.jobbot | grep "state ="

# Stop / start the bot
launchctl bootout    gui/$(id -u)/com.danielhung.jobbot
launchctl bootstrap  gui/$(id -u) ~/Library/LaunchAgents/com.danielhung.jobbot.plist
```

## CRITICAL: code changes don't take effect until kickstart

The running bot keeps the OLD module code in memory. After editing ANY `.py`, you must:

```bash
launchctl kickstart -k gui/$(id -u)/com.danielhung.jobbot
```

Otherwise live behavior won't match what's on disk.

## Architecture (the big picture)

```
Gmail (poll every POLL_INTERVAL_SECONDS, default 600s)
    │
    ▼  gmail_client.iter_messages() — uses Gmail `q:` query in DEFAULT_QUERY
    │  (specific phrases + ATS sender domains; -from: noise filters)
    │
    ▼  classifier.classify(subject, body, from_name, from_addr)
    │  ├── looks_job_related()  → reject newsletters / generic noise
    │  ├── combined dash/at subject patterns first (most reliable: gives co+pos together)
    │  ├── company extraction: subject patterns → ATS `via` → ATS from-name →
    │  │   body lookup → "at <Company>" fallback → from-name → domain
    │  ├── position extraction: subject patterns → body patterns
    │  ├── status: regex on a status priority list (Offer > Reject > Onsite > Phone > OA > Applied)
    │  └── confidence scoring
    │
    ▼  llm_extract.extract_position()  — ONLY if heuristics returned no position
    │  ├── Prefers Gemini (if GEMINI_API_KEY set) — rate-limited to 13 RPM with 429 backoff
    │  └── Falls back to Ollama (if running) — local llama3.2:3b
    │
    ▼  NotionDB.upsert()  — find existing row or create new
    │  ├── _candidates_for_company(): normalized fuzzy match (strips Inc./LLC/etc, suffixes)
    │  ├── Re-application detection: if all candidates are Reject AND new email date > latest close,
    │  │   create fresh row instead of resurrecting the rejected one. Out-of-order Applied emails
    │  │   with dates BEFORE the rejection get merged into the existing closed row.
    │  ├── Position scoring: exact normalized > substring > Unknown matches any non-rejected
    │  └── Session cache (norm_co, norm_pos) → page_id, AND (norm_co, "") → page_id
    │     (handles Notion's eventual consistency for back-to-back emails on the same job)
    │
    ▼  sheet_sync.sync_to_sheet()  — only when upserts > 0 and SHEET_ID set
       Full overwrite of A1:Z10000 each time (cell values blasted, sheet formatting preserved)
```

### State persistence

- **state.db** (SQLite): `processed_messages(message_id, processed_at, action, confidence)` for dedup, and `meta(key, value)` storing `last_check_iso` (the cursor for incremental polling — each pass re-scans with a 2-day overlap to compensate for Gmail's day-granular `after:` filter).
- **gmail_token.json**: cached OAuth refresh token.
- **bot.log**: rotates at 5 MB, keeps 3 backups (~20 MB max).

### Notion DB shape (created by `setup_notion.py`)

| Field | Type | Notes |
|---|---|---|
| Position | title | Locked after create (never overwritten on update unless we go from Unknown → real value) |
| Company | rich_text | Updated on every email |
| Location | rich_text | Only set by sheet import |
| Applied date | date | **Locked after first create** — never overwritten by update |
| Last update | date | Bumped every email |
| Status | select | Applied / OA / Phone / Onsite / Offer / Reject / Ghosted / Withdrew / Needs Review |
| Source | select | LinkedIn / Greenhouse / Lever / Workday / Indeed / Direct / Referral / Other |
| Job link | url | |
| Last email subject | rich_text | |
| Notes | rich_text | Manual only |

## Non-obvious things that will bite you

1. **`load_dotenv()` order matters.** In `bot.py` it MUST run before `import llm_extract` / `import sheet_sync`. Those modules read `GEMINI_API_KEY` / `SHEET_ID` at module load. Reordering breaks the env-var pickup silently — fields just stay empty.

2. **Gmail SCOPES change invalidates tokens.** The current SCOPES include both `gmail.readonly` and `spreadsheets` (for the sheet sync). If you add/remove a scope, `gmail_token.json` becomes invalid and the user must redo OAuth in a browser: `rm gmail_token.json && python bot.py --once`.

3. **Position field is intentionally sticky.** `notion_db.py` `_properties(for_create=False)` will only overwrite Position if the new value is real (not "Unknown") — this protects the human-curated title from being clobbered by later "Position: Unknown" emails.

4. **Applied date is intentionally sticky.** Same module never sets `Applied date` on update. This preserves the original spreadsheet-imported date even when newer emails arrive.

5. **Re-application logic depends on `new_date`.** If the caller passes a status of `Applied`/`OA`/`Phone`/`Onsite`/`Offer` but no `new_date`, the bot defaults to matching the closed row (safer than spurious row creation). Make sure `applied_date` is passed when the new email truly represents a re-application.

6. **Heuristic-first, LLM-only-as-fallback.** `bot.handle_message` calls the LLM ONLY when `result.position` is empty AND `result.company` exists AND there's a body. Don't reorder this — running the LLM on every email burns Gemini quota fast.

7. **Gemini wins when both providers are present.** `llm_extract.extract_position` does NOT fall back to Ollama after a Gemini failure. If Gemini errors, position stays Unknown. This is intentional to avoid 404s on absent Ollama models.

8. **`Position` cleaning rejects job-ID-looking strings.** `_clean_company` returns empty string if the cleaned value starts with `JR123` / `R_456` — `_is_valid_company` then rejects it. Companies whose names legitimately start with that pattern would be incorrectly dropped.

9. **The `_extract_company` capture is case-strict for letter 1.** All `COMPANY_SUBJECT_PATTERNS` use `(?-i:[A-Z])` to force the first character to be uppercase even though the rest of the pattern uses `re.I`. Lowercased captures get rejected by `_is_valid_company`. If you add new patterns, follow the same convention.

10. **`bot.log.1` is intentionally untracked.** It's a rotated log, not source. Don't commit it; don't delete it from git status output.

11. **Auth failures are intentionally fatal-but-clean.** `bot.py` catches `RefreshError` (revoked/expired Gmail token) and `FileNotFoundError` (missing creds JSON), logs a remediation hint to `bot.log`, and exits **0**. The plist's `KeepAlive` is `{SuccessfulExit: false}`, so a clean exit stops launchd from respawning. Recovery requires manual `python bot.py --once` to run the browser OAuth flow, then re-bootstrap. Other crashes still exit non-zero and respawn as before.

## Config (`.env`)

| Key | Default | Purpose |
|---|---|---|
| `NOTION_TOKEN` | — | Notion integration secret |
| `NOTION_DATABASE_ID` | — | Filled by `setup_notion.py` |
| `NOTION_PARENT_PAGE_ID` | — | Only used by `setup_notion.py` |
| `GMAIL_CREDENTIALS_PATH` | `./gmail_credentials.json` | OAuth client JSON |
| `GMAIL_TOKEN_PATH` | `./gmail_token.json` | Cached refresh token |
| `POLL_INTERVAL_SECONDS` | `600` | Bot poll cadence |
| `INITIAL_LOOKBACK_DAYS` | `180` | First-run scan window |
| `USE_LLM_EXTRACTION` | `0` | `1` to enable LLM position fallback |
| `GEMINI_API_KEY` | — | If set, used in preference to Ollama |
| `GEMINI_MODEL` | `gemini-2.5-flash` | |
| `OLLAMA_MODEL` | `llama3.2:3b` | |
| `SHEET_ID` | — | Google Sheets target for mirror; empty → sync skipped |
| `SHEET_TAB` | `Sheet1` | |

## One-off scripts

- `setup_notion.py` — creates the Notion DB. Run once; copy DB ID into `.env`.
- `import_sheet.py <path/to/.xlsx>` — bulk-import historical applications from spreadsheet. Has `--dry-run`. Uses the same `NotionDB.upsert` path so dedup against existing rows works.
