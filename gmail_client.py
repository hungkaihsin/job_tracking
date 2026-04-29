"""
Gmail API helpers: OAuth bootstrap + message fetching.

First run pops a browser consent flow, then caches the refresh token
to GMAIL_TOKEN_PATH so subsequent runs are non-interactive.
"""
import base64
import os
import re
from datetime import datetime, timezone
from email import message_from_bytes
from email.utils import parseaddr, parsedate_to_datetime
from typing import Iterator, Optional

from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def get_service(creds_path: str, token_path: str):
    creds: Optional[Credentials] = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(creds_path):
                raise FileNotFoundError(
                    f"OAuth client JSON not found at {creds_path}. "
                    "Download it from Google Cloud Console (APIs & Services → Credentials → "
                    "Create OAuth client ID → Desktop app)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# Gmail search query — specific phrases only. Single keywords like "interview"
# or "offer" pull in tons of noise (Medium articles, Coursera ads, etc.).
DEFAULT_QUERY = (
    '('
    'subject:"thank you for applying" '
    'OR subject:"thank you for your application" '
    'OR subject:"thank you for your interest" '
    'OR subject:"your application" '
    'OR subject:"application received" '
    'OR subject:"application confirmation" '
    'OR subject:"phone screen" '
    'OR subject:"phone interview" '
    'OR subject:"technical screen" '
    'OR subject:"technical interview" '
    'OR subject:"online assessment" '
    'OR subject:"coding assessment" '
    'OR subject:"interview invitation" '
    'OR subject:"interview request" '
    'OR subject:"final round" '
    'OR subject:"next round" '
    'OR subject:"offer letter" '
    'OR subject:"hackerrank" '
    'OR subject:"codesignal" '
    'OR from:greenhouse.io '
    'OR from:lever.co '
    'OR from:myworkday.com '
    'OR from:myworkdayjobs.com '
    'OR from:ashbyhq.com '
    'OR from:smartrecruiters.com '
    'OR from:icims.com '
    'OR from:jobvite.com'
    ')'
    ' -from:medium.com -from:coursera.org -from:linkedin.com -from:indeed.com'
)


def list_message_ids(service, query: str, max_results: int = 500) -> list[str]:
    ids: list[str] = []
    page_token = None
    while True:
        req = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=min(500, max_results - len(ids)),
            pageToken=page_token,
        )
        resp = req.execute()
        for m in resp.get("messages", []):
            ids.append(m["id"])
        page_token = resp.get("nextPageToken")
        if not page_token or len(ids) >= max_results:
            break
    return ids


def _decode_part(part) -> str:
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    raw = base64.urlsafe_b64decode(data + "==")
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _walk_parts(payload):
    yield payload
    for sub in payload.get("parts", []) or []:
        yield from _walk_parts(sub)


def _extract_body(payload) -> str:
    """Prefer text/plain; fall back to text/html stripped."""
    plain = []
    html = []
    for p in _walk_parts(payload):
        mime = p.get("mimeType", "")
        if mime == "text/plain":
            plain.append(_decode_part(p))
        elif mime == "text/html":
            html.append(_decode_part(p))
    if plain:
        return "\n".join(plain)
    if html:
        soup = BeautifulSoup("\n".join(html), "html.parser")
        return soup.get_text("\n", strip=True)
    return ""


def _first_url(text: str) -> Optional[str]:
    m = re.search(r"https?://[^\s<>\"')]+", text or "")
    return m.group(0) if m else None


def fetch_message(service, msg_id: str) -> dict:
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    payload = msg.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    from_raw = headers.get("from", "")
    from_name, from_addr = parseaddr(from_raw)
    subject = headers.get("subject", "")
    date_str = headers.get("date", "")
    try:
        sent_at = parsedate_to_datetime(date_str).astimezone(timezone.utc)
    except Exception:
        sent_at = datetime.now(timezone.utc)
    body = _extract_body(payload)
    return {
        "id": msg_id,
        "thread_id": msg.get("threadId"),
        "from_name": from_name,
        "from_addr": from_addr,
        "subject": subject,
        "body": body,
        "date": sent_at,
        "first_url": _first_url(body),
        "gmail_link": f"https://mail.google.com/mail/u/0/#inbox/{msg.get('threadId')}",
    }


def iter_messages(service, query: str, since_iso: Optional[str] = None) -> Iterator[dict]:
    q = query
    if since_iso:
        # Gmail uses YYYY/MM/DD for after:
        try:
            d = datetime.fromisoformat(since_iso).strftime("%Y/%m/%d")
            q = f"({query}) after:{d}"
        except ValueError:
            pass
    for mid in list_message_ids(service, q):
        try:
            yield fetch_message(service, mid)
        except Exception as e:
            print(f"[gmail] failed to fetch {mid}: {e}")
