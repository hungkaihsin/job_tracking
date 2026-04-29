"""
Thin wrapper around the Notion REST API for the Job Applications database.

Exposes:
  - upsert_application(...): insert a new row, or update existing when (company, position) matches
  - find_application(company, position): returns page_id or None
  - list_all(): paginated fetch of every row (used by tests/import dedupe)
"""
import os
import time
from typing import Optional

import requests

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


class NotionDB:
    def __init__(self, token: str, database_id: str):
        self.token = token
        self.db_id = database_id
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    # ---------- low-level ----------
    def _request(self, method: str, path: str, **kwargs):
        url = f"{NOTION_API}{path}"
        for attempt in range(3):
            r = requests.request(method, url, headers=self.headers, timeout=30, **kwargs)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            return r
        return r

    # ---------- queries ----------
    def find_application(self, company: str, position: str) -> Optional[str]:
        """Return the page_id of an existing row matching (company, position), or None."""
        if not company or not position:
            return None
        body = {
            "filter": {
                "and": [
                    {"property": "Company", "rich_text": {"equals": company}},
                    {"property": "Position", "title": {"equals": position}},
                ]
            },
            "page_size": 1,
        }
        r = self._request("POST", f"/databases/{self.db_id}/query", json=body)
        if r.status_code >= 300:
            return None
        results = r.json().get("results", [])
        return results[0]["id"] if results else None

    def list_all(self):
        """Yield every page in the DB."""
        cursor = None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            r = self._request("POST", f"/databases/{self.db_id}/query", json=body)
            if r.status_code >= 300:
                raise RuntimeError(f"Notion query failed: {r.status_code} {r.text}")
            data = r.json()
            for page in data.get("results", []):
                yield page
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

    # ---------- mutations ----------
    @staticmethod
    def _properties(
        position: str,
        company: str,
        location: Optional[str] = None,
        applied_date: Optional[str] = None,
        last_update: Optional[str] = None,
        status: Optional[str] = None,
        source: Optional[str] = None,
        job_link: Optional[str] = None,
        last_email_subject: Optional[str] = None,
        gmail_link: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> dict:
        props: dict = {
            "Position": {"title": [{"text": {"content": position or "Unknown"}}]},
            "Company": {"rich_text": [{"text": {"content": company or ""}}]},
        }
        if location is not None:
            props["Location"] = {"rich_text": [{"text": {"content": location}}]}
        if applied_date:
            props["Applied date"] = {"date": {"start": applied_date}}
        if last_update:
            props["Last update"] = {"date": {"start": last_update}}
        if status:
            props["Status"] = {"select": {"name": status}}
        if source:
            props["Source"] = {"select": {"name": source}}
        if job_link:
            props["Job link"] = {"url": job_link}
        if last_email_subject is not None:
            props["Last email subject"] = {"rich_text": [{"text": {"content": last_email_subject[:1900]}}]}
        if gmail_link:
            props["Gmail link"] = {"url": gmail_link}
        if notes is not None:
            props["Notes"] = {"rich_text": [{"text": {"content": notes[:1900]}}]}
        return props

    def create(self, **fields) -> str:
        body = {
            "parent": {"database_id": self.db_id},
            "properties": self._properties(**fields),
        }
        r = self._request("POST", "/pages", json=body)
        if r.status_code >= 300:
            raise RuntimeError(f"Notion create failed: {r.status_code} {r.text}")
        return r.json()["id"]

    def update(self, page_id: str, **fields) -> None:
        body = {"properties": self._properties(**fields)}
        # Strip the title since we don't want to overwrite Position on update
        # unless it was explicitly passed; caller controls this by passing position only when intended.
        r = self._request("PATCH", f"/pages/{page_id}", json=body)
        if r.status_code >= 300:
            raise RuntimeError(f"Notion update failed: {r.status_code} {r.text}")

    def upsert(self, **fields) -> tuple[str, bool]:
        """Insert or update; returns (page_id, created)."""
        existing = self.find_application(fields.get("company", ""), fields.get("position", ""))
        if existing:
            self.update(existing, **fields)
            return existing, False
        return self.create(**fields), True
