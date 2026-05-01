"""
Thin wrapper around the Notion REST API for the Job Applications database.

Exposes:
  - upsert_application(...): insert a new row, or update existing when (company, position) matches
  - find_application(company, position): returns page_id or None
  - list_all(): paginated fetch of every row (used by tests/import dedupe)
"""
import os
import re
import time
from typing import Optional

import requests

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def _normalize_company(c: str) -> str:
    """Canonical form for fuzzy company matching.
    Lowercases, strips corporate suffixes, drops domain extensions."""
    if not c:
        return ""
    s = c.lower().strip()
    s = re.sub(
        r"\s*,?\s*(inc\.?|incorporated|llc|l\.l\.c\.?|corp\.?|corporation|"
        r"ltd\.?|limited|gmbh|co\.?|company|technologies|tech|holdings)\s*$",
        "", s,
    )
    s = re.sub(r"\.(com|io|ai|co|app|net|org|so|xyz)$", "", s)
    s = s.strip(" .,-—–")
    s = re.sub(r"\s+", " ", s)
    return s


def _normalize_position(p: str) -> str:
    """Canonical form for fuzzy position matching — strips job IDs and common variations."""
    if not p:
        return ""
    s = p.lower().strip()
    # Strip leading job-IDs (e.g. "JR2011517 Software Engineer" -> "Software Engineer")
    s = re.sub(r"^(?:jr|r[-_]?|req|job)\s*[\-#]?\s*\d{3,}\s*[\-,:.]?\s*", "", s)
    # Strip trailing job-IDs in parens or after dash (e.g. "Data Scientist (622164)" -> "Data Scientist")
    s = re.sub(r"\s*[\-(]+\s*\d{4,}\s*[\)]?\s*$", "", s)
    # Common abbreviations -> expanded form for better matching
    s = re.sub(r"\bml\b", "machine learning", s)
    s = re.sub(r"\bai\b", "artificial intelligence", s)
    s = re.sub(r"\bswe\b", "software engineer", s)
    s = re.sub(r"\bsde\b", "software development engineer", s)
    # Normalize whitespace
    s = re.sub(r"\s+", " ", s)
    return s.strip()


class NotionDB:
    def __init__(self, token: str, database_id: str):
        self.token = token
        self.db_id = database_id
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        # In-memory cache of (normalized_company, normalized_position) -> page_id for
        # rows we created/updated during this session. Notion's API has eventual
        # consistency — without this, two emails for the same job processed back-to-back
        # would each query a stale view and both end up creating new rows.
        self._session_cache: dict[tuple[str, str], str] = {}

    # ---------- low-level ----------
    def _request(self, method: str, path: str, **kwargs):
        url = f"{NOTION_API}{path}"
        last_exc = None
        for attempt in range(4):
            try:
                r = requests.request(method, url, headers=self.headers, timeout=60, **kwargs)
            except (requests.Timeout, requests.ConnectionError) as e:
                last_exc = e
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            return r
        if last_exc:
            raise last_exc
        return r

    # ---------- queries ----------
    @staticmethod
    def _page_text(page: dict, prop_name: str) -> str:
        prop = page.get("properties", {}).get(prop_name, {})
        if not prop:
            return ""
        if "title" in prop and prop["title"]:
            return prop["title"][0].get("plain_text", "")
        if "rich_text" in prop and prop["rich_text"]:
            return prop["rich_text"][0].get("plain_text", "")
        return ""

    @staticmethod
    def _page_select(page: dict, prop_name: str) -> str:
        sel = page.get("properties", {}).get(prop_name, {}).get("select")
        return sel["name"] if sel else ""

    @staticmethod
    def _page_date(page: dict, prop_name: str) -> str:
        d = page.get("properties", {}).get(prop_name, {}).get("date")
        return d["start"] if d else ""

    def _candidates_for_company(self, company: str) -> list[dict]:
        """Find all rows that could plausibly match `company`, using normalized
        comparison. Notion's `equals`/`contains` filters are case-sensitive, so
        we cast a wide net via `contains` on a stable token, then filter
        client-side using `_normalize_company`."""
        normalized = _normalize_company(company)
        if not normalized:
            return []

        # Use the first word of the normalized form as a stable search token.
        token = normalized.split()[0] if normalized else ""
        if len(token) < 2:
            return []

        # Notion's `contains` is case-sensitive; try a few capitalizations.
        seen_ids: set[str] = set()
        candidates: list[dict] = []
        for variant in {token.capitalize(), token, token.upper(), company}:
            if not variant:
                continue
            body = {
                "filter": {"property": "Company", "rich_text": {"contains": variant}},
                "page_size": 50,
            }
            r = self._request("POST", f"/databases/{self.db_id}/query", json=body)
            if r.status_code >= 300:
                continue
            for p in r.json().get("results", []):
                if p["id"] in seen_ids:
                    continue
                # Confirm the row's normalized company matches our target.
                row_co = self._page_text(p, "Company")
                if _normalize_company(row_co) == normalized:
                    seen_ids.add(p["id"])
                    candidates.append(p)
        return candidates

    def find_application(
        self,
        company: str,
        position: str,
        new_status: Optional[str] = None,
        new_date: Optional[str] = None,
    ) -> Optional[str]:
        """Find a matching row using normalized company comparison.

        Logic:
        1. Get all rows whose normalized company == normalized(input).
        2. RE-APPLICATION DETECTION: if the new email is a fresh "Applied" / OA / Phone /
           Onsite / Offer signal, exclude any candidate whose status is already "Reject" —
           a rejected application that re-opens means the user is re-applying, so it
           gets a fresh row instead of overwriting historical reject.
        3. If position is real, prefer position match (normalized equality, then substring).
        4. Otherwise (position Unknown), prefer non-rejected rows, then most recent.
        5. If no candidates, return None (caller will create a new row).
        """
        if not company:
            return None

        # Session cache check first — handles the "two emails for the same job
        # processed back-to-back" race where Notion's eventual consistency would
        # cause the second query to miss the just-created row.
        cache_key = (_normalize_company(company), _normalize_position(position or ""))
        if cache_key in self._session_cache:
            return self._session_cache[cache_key]

        candidates = self._candidates_for_company(company)
        if not candidates:
            return None

        # Re-application: a new active-status email should not merge into a closed row,
        # BUT only if the new email is genuinely AFTER the existing rejection (otherwise
        # it's the original confirmation arriving out-of-order and should match the row).
        ACTIVE = {"Applied", "OA", "Phone", "Onsite", "Offer"}
        if new_status in ACTIVE:
            non_rejected = [c for c in candidates if self._page_select(c, "Status") != "Reject"]
            if non_rejected:
                candidates = non_rejected
            elif new_date:
                # All candidates are Rejected. Compare dates: if the email is AFTER
                # the most recent close, it's a real re-application.
                latest_close = max(
                    (
                        self._page_date(c, "Last update")
                        or self._page_date(c, "Applied date")
                        or "0000-00-00"
                    )
                    for c in candidates
                )
                if new_date > latest_close:
                    return None  # Genuine re-application — create fresh row
                # else fall through and match the closed row (out-of-order email)
            # If no new_date provided, match the closed row (safer default)

        # Score and rank candidates
        norm_pos = _normalize_position(position) if position and position != "Unknown" else ""

        def score(p: dict) -> tuple:
            existing_pos_norm = _normalize_position(self._page_text(p, "Position"))
            existing_status = self._page_select(p, "Status")
            existing_date = self._page_date(p, "Applied date") or "0000-00-00"

            # Position score
            pos_score = 0
            if norm_pos and existing_pos_norm:
                if existing_pos_norm == norm_pos:
                    pos_score = 100
                elif norm_pos in existing_pos_norm or existing_pos_norm in norm_pos:
                    pos_score = 50
            elif not norm_pos:
                pos_score = 10

            status_score = 5 if existing_status != "Reject" else 0
            return (pos_score, status_score, existing_date)

        candidates.sort(key=score, reverse=True)
        if norm_pos:
            best = candidates[0]
            if score(best)[0] < 50:
                return None
            return best["id"]
        return candidates[0]["id"]

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
        for_create: bool,
        position: str,
        company: str,
        location: Optional[str] = None,
        applied_date: Optional[str] = None,
        last_update: Optional[str] = None,
        status: Optional[str] = None,
        source: Optional[str] = None,
        job_link: Optional[str] = None,
        last_email_subject: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> dict:
        props: dict = {}
        if for_create:
            # Title field is required at create time.
            props["Position"] = {"title": [{"text": {"content": position or "Unknown"}}]}
            props["Company"] = {"rich_text": [{"text": {"content": company or ""}}]}
            # Applied date only on create — preserves spreadsheet date on later email updates.
            if applied_date:
                props["Applied date"] = {"date": {"start": applied_date}}
        else:
            # On update: only overwrite Position if we have a real value (not Unknown).
            if position and position != "Unknown":
                props["Position"] = {"title": [{"text": {"content": position}}]}
            if company:
                props["Company"] = {"rich_text": [{"text": {"content": company}}]}
            # Skip applied_date on update — never clobber the original.

        if location is not None and location != "":
            props["Location"] = {"rich_text": [{"text": {"content": location}}]}
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
        if notes is not None:
            props["Notes"] = {"rich_text": [{"text": {"content": notes[:1900]}}]}
        return props

    def create(self, **fields) -> str:
        body = {
            "parent": {"database_id": self.db_id},
            "properties": self._properties(for_create=True, **fields),
        }
        r = self._request("POST", "/pages", json=body)
        if r.status_code >= 300:
            raise RuntimeError(f"Notion create failed: {r.status_code} {r.text}")
        return r.json()["id"]

    def update(self, page_id: str, **fields) -> None:
        body = {"properties": self._properties(for_create=False, **fields)}
        if not body["properties"]:
            return  # nothing to update
        r = self._request("PATCH", f"/pages/{page_id}", json=body)
        if r.status_code >= 300:
            raise RuntimeError(f"Notion update failed: {r.status_code} {r.text}")

    def upsert(self, **fields) -> tuple[str, bool]:
        """Insert or update; returns (page_id, created)."""
        company = fields.get("company", "")
        position = fields.get("position", "")
        existing = self.find_application(
            company,
            position,
            new_status=fields.get("status"),
            new_date=fields.get("applied_date"),
        )
        if existing:
            self.update(existing, **fields)
            page_id = existing
            created = False
        else:
            page_id = self.create(**fields)
            created = True
        # Cache by exact (norm_co, norm_pos) AND by company-only key so a
        # follow-up email with Unknown position still finds this row.
        norm_co = _normalize_company(company)
        norm_pos = _normalize_position(position)
        self._session_cache[(norm_co, norm_pos)] = page_id
        if norm_pos:
            # Also register the company-only key so an Unknown-position email matches.
            self._session_cache.setdefault((norm_co, ""), page_id)
        return page_id, created
