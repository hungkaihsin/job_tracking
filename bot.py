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
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

import classifier
import gmail_client
from notion_db import NotionDB

load_dotenv()

# ---------- logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(os.path.dirname(__file__), "bot.log")),
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
def run_once(state, gmail_svc, db: NotionDB) -> int:
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

    is_low_conf = result.confidence < CONF_THRESHOLD or not result.company or not result.position

    fields = {
        "company": result.company or "Unknown",
        "position": result.position or msg["subject"][:80] or "Unknown",
        "status": result.status or ("Needs Review" if is_low_conf else "Applied"),
        "source": result.source or "Other",
        "last_update": msg["date"].date().isoformat(),
        "last_email_subject": msg["subject"],
        "gmail_link": msg["gmail_link"],
    }
    if msg.get("first_url"):
        fields["job_link"] = msg["first_url"]

    if is_low_conf:
        fields["status"] = "Needs Review"

    # Only set Applied date if we're creating a fresh row from an "Applied" email
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
    state = open_state()
    gmail_svc = gmail_client.get_service(GMAIL_CREDS, GMAIL_TOKEN)
    db = NotionDB(NOTION_TOKEN, DB_ID)

    if args.once:
        run_once(state, gmail_svc, db)
        return

    while True:
        try:
            run_once(state, gmail_svc, db)
        except Exception as e:
            log.exception("Pass failed: %s", e)
        log.info("Sleeping %ds...", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
