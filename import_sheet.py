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

import openpyxl
from dotenv import load_dotenv

from notion_db import NotionDB

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
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    header = [normalize(c) for c in rows[0][:5]]
    if header != EXPECTED_HEADERS:
        print(f"WARNING: header is {header}, expected {EXPECTED_HEADERS}. Continuing anyway.")
    out = []
    for raw in rows[1:]:
        date_v, position, company, location, result = (raw + (None,) * 5)[:5]
        position = normalize(position)
        company = normalize(company)
        if not position and not company:
            continue
        applied_date = None
        if isinstance(date_v, datetime):
            applied_date = date_v.date().isoformat()
        elif isinstance(date_v, str) and date_v.strip():
            try:
                applied_date = datetime.fromisoformat(date_v.strip()).date().isoformat()
            except ValueError:
                pass
        out.append(
            {
                "position": position or "Unknown",
                "company": company or "Unknown",
                "location": normalize(location),
                "applied_date": applied_date,
                "status": map_result_to_status(result),
                "source": "Other",
            }
        )
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("xlsx", help="Path to Job_Hunt_Tracking_2026.xlsx")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    token = os.environ["NOTION_TOKEN"]
    db_id = os.environ.get("NOTION_DATABASE_ID", "").strip()
    if not db_id:
        sys.exit("ERROR: NOTION_DATABASE_ID missing — run setup_notion.py first")

    rows = parse_rows(args.xlsx)
    print(f"Parsed {len(rows)} rows from {args.xlsx}")
    if args.dry_run:
        for r in rows[:10]:
            print(r)
        print("...(dry run, no Notion writes)")
        return

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
