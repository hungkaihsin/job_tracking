"""
One-time import: read Job_Hunt_Tracking_2026.xlsx and seed the Notion DB.

Usage:
    python import_sheet.py /path/to/Job_Hunt_Tracking_2026.xlsx [--dry-run]

Notes:
- Skips rows already in Notion (matched on Company + Position).
- Empty Result -> Status "Applied" (we assume it's an open application).
- "Reject" -> Status "Reject".
- Empty Date -> left blank; you can backfill in Notion.
"""
import argparse
import os
import sys
from datetime import datetime
from urllib.parse import urlparse, urlunparse

import openpyxl
from dotenv import load_dotenv

from notion_db import NotionDB


def clean_url(url: str | None) -> str | None:
    """Strip tracking query strings; fall back to truncation for safety."""
    if not url:
        return None
    try:
        parts = urlparse(url)
        # Drop query + fragment for known-noisy hosts (LinkedIn, Indeed) — keeps it short and stable.
        if any(h in (parts.netloc or "").lower() for h in ("linkedin.com", "indeed.com")):
            cleaned = urlunparse((parts.scheme, parts.netloc, parts.path, "", "", ""))
            return cleaned
    except Exception:
        pass
    # Notion URL property max is 2000 chars; safety truncate.
    if len(url) > 2000:
        return url[:2000]
    return url

load_dotenv()

EXPECTED_HEADERS = ["Data", "Position", "Company", "Location", "Result"]


def normalize(s):
    if s is None:
        return ""
    return str(s).strip()


def map_result_to_status(result: str) -> str:
    r = normalize(result).lower()
    if not r:
        return "Applied"
    if "reject" in r:
        return "Reject"
    if "offer" in r:
        return "Offer"
    if "ghost" in r:
        return "Ghosted"
    if "withdr" in r:
        return "Withdrew"
    if "onsite" in r or "final" in r:
        return "Onsite"
    if "phone" in r or "screen" in r:
        return "Phone"
    if "oa" in r or "assess" in r:
        return "OA"
    return "Applied"


def parse_rows(xlsx_path: str):
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=False))  # Cell objects, so we can read hyperlinks
    if not rows:
        return []
    header = [normalize(c.value) for c in rows[0][:5]]
    if header != EXPECTED_HEADERS:
        print(f"WARNING: header is {header}, expected {EXPECTED_HEADERS}. Continuing anyway.")
    out = []
    current_date = None  # carry-forward: merged cells return None on every row but the first
    for row in rows[1:]:
        cells = (list(row) + [None] * 5)[:5]
        date_cell, position_cell, company_cell, location_cell, result_cell = cells
        date_v = date_cell.value if date_cell is not None else None
        position = normalize(position_cell.value if position_cell is not None else None)
        company = normalize(company_cell.value if company_cell is not None else None)
        location = normalize(location_cell.value if location_cell is not None else None)
        result = result_cell.value if result_cell is not None else None
        if not position and not company:
            continue
        # If this row has its own date, update the carry-forward.
        if isinstance(date_v, datetime):
            current_date = date_v.date().isoformat()
        elif isinstance(date_v, str) and date_v.strip():
            try:
                current_date = datetime.fromisoformat(date_v.strip()).date().isoformat()
            except ValueError:
                pass
        # Extract hyperlink from the Position cell (your spreadsheet has links on the title).
        job_link = None
        if position_cell is not None and position_cell.hyperlink is not None:
            job_link = clean_url(position_cell.hyperlink.target)
        out.append(
            {
                "position": position or "Unknown",
                "company": company or "Unknown",
                "location": location,
                "applied_date": current_date,  # inherits from last dated row above
                "status": map_result_to_status(result),
                "source": "Other",
                "job_link": job_link,
            }
        )
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("xlsx", help="Path to Job_Hunt_Tracking_2026.xlsx")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # Dry-run only needs the xlsx — no Notion access required.
    rows = parse_rows(args.xlsx)
    print(f"Parsed {len(rows)} rows from {args.xlsx}")
    if args.dry_run:
        for r in rows[:10]:
            print(r)
        if len(rows) > 10:
            print(f"...({len(rows) - 10} more rows)")
        print("(dry run, no Notion writes)")
        return

    token = os.environ.get("NOTION_TOKEN", "").strip()
    db_id = os.environ.get("NOTION_DATABASE_ID", "").strip()
    if not token:
        sys.exit("ERROR: NOTION_TOKEN missing in .env")
    if not db_id:
        sys.exit("ERROR: NOTION_DATABASE_ID missing — run setup_notion.py first")

    db = NotionDB(token, db_id)
    created = updated = 0
    for i, r in enumerate(rows, 1):
        page_id, was_created = db.upsert(**r)
        if was_created:
            created += 1
        else:
            updated += 1
        if i % 10 == 0:
            print(f"  {i}/{len(rows)} (created={created} updated={updated})")
    print(f"Done. Created {created}, updated {updated}.")


if __name__ == "__main__":
    main()
