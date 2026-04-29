"""
One-time setup: create the "Job Applications" database in Notion.

Usage:
    python setup_notion.py

After it runs, copy the printed database ID into .env as NOTION_DATABASE_ID.
"""
import os
import re
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
PARENT_RAW = os.environ.get("NOTION_PARENT_PAGE_ID", "").strip()

if not NOTION_TOKEN:
    sys.exit("ERROR: NOTION_TOKEN missing in .env")
if not PARENT_RAW:
    sys.exit("ERROR: NOTION_PARENT_PAGE_ID missing in .env (paste the page URL or 32-char ID)")


def extract_page_id(raw: str) -> str:
    """Accept either a raw 32-char ID, a dashed UUID, or a notion.so URL."""
    # Try to find a 32-char hex run
    m = re.search(r"([0-9a-fA-F]{32})", raw.replace("-", ""))
    if not m:
        sys.exit(f"ERROR: could not find a Notion page ID in: {raw}")
    hex32 = m.group(1)
    return f"{hex32[0:8]}-{hex32[8:12]}-{hex32[12:16]}-{hex32[16:20]}-{hex32[20:32]}"


PARENT_ID = extract_page_id(PARENT_RAW)

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# Schema: Position is the title (Notion requires exactly one title field per DB)
SCHEMA = {
    "parent": {"type": "page_id", "page_id": PARENT_ID},
    "icon": {"type": "emoji", "emoji": "💼"},
    "title": [{"type": "text", "text": {"content": "Job Applications"}}],
    "properties": {
        "Position": {"title": {}},
        "Company": {"rich_text": {}},
        "Location": {"rich_text": {}},
        "Applied date": {"date": {}},
        "Last update": {"date": {}},
        "Status": {
            "select": {
                "options": [
                    {"name": "Applied", "color": "blue"},
                    {"name": "OA", "color": "purple"},
                    {"name": "Phone", "color": "yellow"},
                    {"name": "Onsite", "color": "orange"},
                    {"name": "Offer", "color": "green"},
                    {"name": "Reject", "color": "red"},
                    {"name": "Ghosted", "color": "gray"},
                    {"name": "Withdrew", "color": "brown"},
                    {"name": "Needs Review", "color": "pink"},
                ]
            }
        },
        "Source": {
            "select": {
                "options": [
                    {"name": "LinkedIn", "color": "blue"},
                    {"name": "Greenhouse", "color": "green"},
                    {"name": "Lever", "color": "purple"},
                    {"name": "Workday", "color": "orange"},
                    {"name": "Indeed", "color": "yellow"},
                    {"name": "Direct", "color": "gray"},
                    {"name": "Referral", "color": "pink"},
                    {"name": "Other", "color": "default"},
                ]
            }
        },
        "Job link": {"url": {}},
        "Last email subject": {"rich_text": {}},
        "Notes": {"rich_text": {}},
    },
}


def main():
    print(f"Creating database under parent page {PARENT_ID}...")
    r = requests.post("https://api.notion.com/v1/databases", headers=HEADERS, json=SCHEMA)
    if r.status_code >= 300:
        print("ERROR creating database:")
        print(r.status_code, r.text)
        sys.exit(1)
    data = r.json()
    db_id = data["id"]
    print()
    print("✅ Database created.")
    print(f"   ID: {db_id}")
    print(f"   URL: {data.get('url')}")
    print()
    print("Next: copy this line into your .env:")
    print(f"   NOTION_DATABASE_ID={db_id}")


if __name__ == "__main__":
    main()
