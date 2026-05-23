"""
Job application tracker bot.

Long-running process (mirroring money_tracking/bot.py pattern):
  - Polls Gmail every POLL_INTERVAL_SECONDS
  - Classifies each new message with heuristics
  - Upserts into the Notion "Job Applications" DB
  - Logs everything to bot.log; state.db tracks processed message IDs

Run via launchd (com.danielhung.jobbot.plist) so it autostarts on login
and keeps running. To run manually for testing:
    python bot.py            # runs the loop
    python bot.py --once     # one pass, then exit (good for debugging)
"""
import argparse
import logging
import logging.handlers
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

# IMPORTANT: load_dotenv MUST run before importing llm_extract / sheet_sync,
# because those modules read env vars (GEMINI_API_KEY, SHEET_ID) at module load.
from dotenv import load_dotenv
load_dotenv()

from google.auth.exceptions import RefreshError

import classifier
import gmail_client
import llm_extract
import sheet_sync
from notion_db import NotionDB

# ---------- logging ----------
_LOG_PATH = os.path.join(os.path.dirname(__file__), "bot.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        # Rotate at 5 MB, keep 3 backups (bot.log + bot.log.1..3 = max ~20 MB).
        logging.handlers.RotatingFileHandler(
            _LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3
        ),
    ],
)
log = logging.getLogger("jobbot")

# ---------- config ----------
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DB_ID = os.environ["NOTION_DATABASE_ID"]
GMAIL_CREDS = os.environ.get("GMAIL_CREDENTIALS_PATH", "./gmail_credentials.json")
GMAIL_TOKEN = os.environ.get("GMAIL_TOKEN_PATH", "./gmail_token.json")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "600"))
INITIAL_LOOKBACK_DAYS = int(os.environ.get("INITIAL_LOOKBACK_DAYS", "180"))
STATE_PATH = os.path.join(os.path.dirname(__file__), "state.db")

CONF_THRESHOLD = 0.55  # below this -> "Needs Review"

# Optional Ollama-based position extraction for emails the heuristic classifier
# couldn't fully parse. Heuristics run first; LLM only fills gaps.
USE_LLM_EXTRACTION = os.environ.get("USE_LLM_EXTRACTION", "0") == "1"
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", llm_extract.DEFAULT_MODEL)
_LLM_OK = False  # set in main() at startup


# ---------- state DB (sqlite) ----------
def open_state() -> sqlite3.Connection:
    conn = sqlite3.connect(STATE_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS processed_messages ("
        " message_id TEXT PRIMARY KEY,"
        " processed_at TEXT NOT NULL,"
        " action TEXT,"
        " confidence REAL"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta ("
        " key TEXT PRIMARY KEY,"
        " value TEXT"
        ")"
    )
    conn.commit()
    return conn


def already_processed(conn, mid: str) -> bool:
    cur = conn.execute("SELECT 1 FROM processed_messages WHERE message_id = ?", (mid,))
    return cur.fetchone() is not None


def mark_processed(conn, mid: str, action: str, confidence: float):
    conn.execute(
        "INSERT OR REPLACE INTO processed_messages (message_id, processed_at, action, confidence)"
        " VALUES (?, ?, ?, ?)",
        (mid, datetime.now(timezone.utc).isoformat(), action, confidence),
    )
    conn.commit()


def get_meta(conn, key: str) -> str | None:
    cur = conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
    row = cur.fetchone()
    return row[0] if row else None


def set_meta(conn, key: str, value: str):
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


# ---------- main pass ----------
def run_once(state, gmail_svc, db: NotionDB, gmail_creds=None) -> int:
    last_seen = get_meta(state, "last_check_iso")
    if last_seen is None:
        # First run: look back INITIAL_LOOKBACK_DAYS
        since = datetime.now(timezone.utc) - timedelta(days=INITIAL_LOOKBACK_DAYS)
        since_iso = since.date().isoformat()
        log.info("First run — scanning last %d days (since %s)", INITIAL_LOOKBACK_DAYS, since_iso)
    else:
        # Re-scan a small overlap window (Gmail's `after:` is day-granular)
        since = datetime.fromisoformat(last_seen) - timedelta(days=2)
        since_iso = since.date().isoformat()

    new_count = 0
    skip_count = 0
    review_count = 0
    upserts = 0

    for msg in gmail_client.iter_messages(gmail_svc, gmail_client.DEFAULT_QUERY, since_iso=since_iso):
        if already_processed(state, msg["id"]):
            skip_count += 1
            continue
        new_count += 1
        try:
            handle_message(msg, db, state)
            upserts += 1
        except Exception as e:
            log.exception("Error handling %s: %s", msg["id"], e)
            continue

    set_meta(state, "last_check_iso", datetime.now(timezone.utc).isoformat())
    log.info("Pass done: new=%d skipped=%d upserts=%d", new_count, skip_count, upserts)

    # Mirror Notion -> Google Sheet whenever something changed.
    if upserts > 0 and gmail_creds and sheet_sync.SHEET_ID:
        err = sheet_sync.sync_to_sheet(gmail_creds, db)
        if err:
            log.warning("Sheet sync skipped: %s", err)

    return new_count


def handle_message(msg: dict, db: NotionDB, state: sqlite3.Connection):
    result = classifier.classify(
        subject=msg["subject"],
        body=msg["body"],
        from_name=msg["from_name"],
        from_addr=msg["from_addr"],
    )
    if not result.is_job_related:
        mark_processed(state, msg["id"], "ignored", 0.0)
        return

    # LLM fallback: when heuristics couldn't extract a position but we have a
    # company and a body, ask Ollama to read the email. Local, free, ~3s/email.
    position = result.position
    if not position and _LLM_OK and result.company and msg.get("body"):
        try:
            llm_pos = llm_extract.extract_position(
                subject=msg["subject"],
                body=msg["body"],
                company=result.company,
                model=OLLAMA_MODEL,
            )
            if llm_pos:
                position = llm_pos
                log.info("LLM filled position: %r for %s @ %s", llm_pos, msg["subject"][:40], result.company)
        except Exception as e:
            log.warning("LLM extract failed: %s", e)

    # Only flag for manual review if we couldn't even identify the company.
    # Position-Unknown is fine — many ATS confirmations don't mention the role.
    needs_review = not result.company

    fields = {
        "company": result.company or "Unknown",
        "position": position or "Unknown",
        "status": result.status or ("Needs Review" if needs_review else "Applied"),
        "source": result.source or "Other",
        "last_update": msg["date"].date().isoformat(),
        "last_email_subject": msg["subject"],
    }
    if msg.get("first_url"):
        fields["job_link"] = msg["first_url"]

    if needs_review:
        fields["status"] = "Needs Review"

    # Only set Applied date when we're creating a fresh row from an "Applied" email.
    # On update, notion_db.py skips Applied date so we don't clobber the spreadsheet date.
    if result.status == "Applied":
        fields["applied_date"] = msg["date"].date().isoformat()

    page_id, created = db.upsert(**fields)
    action = "created" if created else "updated"
    log.info(
        "%s: %s | %s @ %s -> %s (conf=%.2f, %s)",
        action, msg["subject"][:60], fields["position"][:40], fields["company"],
        fields["status"], result.confidence, result.reason,
    )
    mark_processed(state, msg["id"], action, result.confidence)


# ---------- entrypoint ----------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run one pass and exit")
    args = parser.parse_args()

    log.info("jobbot starting (poll=%ds, db=%s...)", POLL_INTERVAL, DB_ID[:8])

    # Optional LLM: prefer Gemini if API key present; otherwise check Ollama.
    global _LLM_OK
    if USE_LLM_EXTRACTION:
        if llm_extract.GEMINI_API_KEY:
            _LLM_OK = True
            log.info("Gemini API enabled — LLM position fallback (model=%s)", llm_extract.GEMINI_MODEL)
        elif llm_extract.is_ollama_available():
            _LLM_OK = True
            log.info("Ollama available — LLM position fallback enabled (model=%s)", OLLAMA_MODEL)
        else:
            log.warning(
                "USE_LLM_EXTRACTION=1 but no provider available "
                "(set GEMINI_API_KEY or run `ollama serve`). Falling back to heuristic-only."
            )

    state = open_state()
    from googleapiclient.discovery import build as gapi_build
    # Auth errors here are PERMANENT (token revoked / creds file missing) —
    # exit 0 so launchd's KeepAlive=SuccessfulExit-false stops respawning us.
    # Recover by running `python bot.py --once` manually for browser OAuth.
    try:
        gmail_creds = gmail_client.get_credentials(GMAIL_CREDS, GMAIL_TOKEN)
    except RefreshError as e:
        log.error(
            "Gmail OAuth token revoked or expired: %s. "
            "Fix: rm %s && python bot.py --once (browser OAuth required). "
            "Exiting cleanly; launchd will not respawn.",
            e, GMAIL_TOKEN,
        )
        sys.exit(0)
    except FileNotFoundError as e:
        log.error("Gmail OAuth credentials missing: %s. Exiting cleanly.", e)
        sys.exit(0)
    gmail_svc = gapi_build("gmail", "v1", credentials=gmail_creds, cache_discovery=False)
    db = NotionDB(NOTION_TOKEN, DB_ID)

    if args.once:
        run_once(state, gmail_svc, db, gmail_creds=gmail_creds)
        return

    while True:
        try:
            run_once(state, gmail_svc, db, gmail_creds=gmail_creds)
        except Exception as e:
            log.exception("Pass failed: %s", e)
        log.info("Sleeping %ds...", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
