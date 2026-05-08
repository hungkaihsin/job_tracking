"""
Mirror the Notion job applications DB to a Google Sheet.

Runs at the end of each bot pass (when there were upserts) so the sheet
always reflects the latest Notion state. Uses the same OAuth credentials
as Gmail — the SCOPES list in gmail_client.py includes 'spreadsheets'.

Layout: full overwrite of a fixed range each sync. Preserves sheet-level
formatting (filters, freezes, conditional formatting) but blasts cell
values. Don't manually edit values in this sheet — edit Notion instead.
"""
import logging
import os
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger("jobbot.sheet")

SHEET_ID = os.environ.get("SHEET_ID", "").strip()
SHEET_TAB = os.environ.get("SHEET_TAB", "Sheet1").strip()

HEADERS = [
    "Applied date",
    "Position",
    "Company",
    "Location",
    "Status",
    "Last update",
    "Source",
    "Job link",
    "Last email subject",
    "Notes",
]


def _row_for_page(page: dict) -> list:
    """Convert a Notion page (from query results) into a sheet row."""
    props = page.get("properties", {})

    def text(name):
        prop = props.get(name) or {}
        if "title" in prop and prop["title"]:
            return prop["title"][0].get("plain_text", "")
        if "rich_text" in prop and prop["rich_text"]:
            return prop["rich_text"][0].get("plain_text", "")
        return ""

    def select(name):
        prop = props.get(name) or {}
        sel = prop.get("select")
        return sel["name"] if sel else ""

    def date(name):
        prop = props.get(name) or {}
        d = prop.get("date")
        return d["start"] if d else ""

    def url(name):
        prop = props.get(name) or {}
        return prop.get("url") or ""

    return [
        date("Applied date"),
        text("Position"),
        text("Company"),
        text("Location"),
        select("Status"),
        date("Last update"),
        select("Source"),
        url("Job link"),
        text("Last email subject"),
        text("Notes"),
    ]


def sync_to_sheet(creds, notion_db) -> Optional[str]:
    """Pull all rows from Notion DB and overwrite the sheet.

    Returns an error message on failure, None on success.
    """
    if not SHEET_ID:
        return "SHEET_ID not configured in .env"

    # 1. Fetch all rows from Notion (sorted by Applied date desc)
    rows = []
    cursor = None
    while True:
        body = {
            "page_size": 100,
            "sorts": [{"property": "Applied date", "direction": "descending"}],
        }
        if cursor:
            body["start_cursor"] = cursor
        r = notion_db._request("POST", f"/databases/{notion_db.db_id}/query", json=body)
        if r.status_code >= 300:
            return f"Notion query failed: {r.status_code} {r.text[:200]}"
        data = r.json()
        rows.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    # 2. Convert to 2D array (header row + data)
    values = [HEADERS]
    for page in rows:
        values.append(_row_for_page(page))

    # 3. Write to sheet
    try:
        service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        # Clear the existing range first so old rows beyond the new data are wiped.
        service.spreadsheets().values().clear(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_TAB}!A1:Z10000",
        ).execute()
        # Write new data starting at A1.
        service.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_TAB}!A1",
            valueInputOption="USER_ENTERED",  # parses dates/links naturally
            body={"values": values},
        ).execute()
    except HttpError as e:
        if e.resp.status == 403 or e.resp.status == 401:
            return (
                f"Sheets API auth error ({e.resp.status}). "
                "Did you re-OAuth after adding the spreadsheets scope? "
                "Try: rm gmail_token.json && python bot.py --once"
            )
        return f"Sheets API error: {e}"
    except Exception as e:
        return f"Unexpected sheet sync error: {e}"

    log.info("Synced %d rows to Google Sheet (%s)", len(rows), SHEET_ID[:8])
    return None
