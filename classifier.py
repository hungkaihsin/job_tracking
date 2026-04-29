"""
Heuristics-based classifier for job-related emails.

Returns a ClassifyResult with confidence — bot.py routes low-confidence
items to "Needs Review" instead of guessing.

Phase 1: pure heuristics (this file).
Phase 2 (later): hybrid — local Ollama LLM fallback for low-confidence cases.
"""
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class ClassifyResult:
    is_job_related: bool
    company: Optional[str] = None
    position: Optional[str] = None
    status: Optional[str] = None
    source: Optional[str] = None
    confidence: float = 0.0   # 0.0 .. 1.0
    reason: str = ""          # for the log


# ---------- ATS / source detection by sender domain ----------
ATS_DOMAINS = {
    "greenhouse.io": "Greenhouse",
    "myworkday.com": "Workday",
    "myworkdayjobs.com": "Workday",
    "lever.co": "Lever",
    "hire.lever.co": "Lever",
    "smartrecruiters.com": "Other",
    "icims.com": "Other",
    "ashbyhq.com": "Other",
    "jobvite.com": "Other",
    "linkedin.com": "LinkedIn",
    "indeed.com": "Indeed",
}

# ---------- Status keywords (ordered: more specific first) ----------
STATUS_PATTERNS = [
    ("Offer", [
        r"\bwe(?:'re| are) (?:thrilled|excited|pleased) to (?:offer|extend)",
        r"\boffer letter\b",
        r"\bextend(?:ing)? you an offer\b",
        r"\bpleased to offer\b",
    ]),
    ("Reject", [
        r"\bunfortunately\b.*\b(?:not|other|decided)\b",
        r"\bmove forward with other candidates\b",
        r"\bnot moving forward\b",
        r"\bafter careful (?:consideration|review).*not\b",
        r"\bregret to inform\b",
        r"\bdecided not to (?:move|proceed)\b",
        r"\bwill not be moving forward\b",
    ]),
    ("Onsite", [
        r"\bonsite (?:interview|round)\b",
        r"\bfinal (?:round|interview)\b",
        r"\bsuper ?day\b",
        r"\bvirtual onsite\b",
    ]),
    ("Phone", [
        r"\bphone (?:screen|interview|call)\b",
        r"\btechnical (?:screen|phone)\b",
        r"\brecruiter (?:screen|call|chat)\b",
        r"\binitial (?:call|conversation|screen)\b",
        r"\bschedule (?:a |an )?(?:call|chat|interview)\b",
    ]),
    ("OA", [
        r"\bonline assessment\b",
        r"\bcoding (?:assessment|challenge)\b",
        r"\b(?:hackerrank|codesignal|coderpad|codility)\b",
        r"\btake[- ]home (?:assignment|test)\b",
    ]),
    ("Applied", [
        r"\bthank(?:s| you) for (?:applying|your application|your interest)\b",
        r"\bwe(?:'ve| have) received your application\b",
        r"\bapplication (?:received|submitted|confirmation)\b",
        r"\byour application (?:to|for|has been received)\b",
    ]),
]

# ---------- Position extraction patterns (subject is most reliable) ----------
POSITION_SUBJECT_PATTERNS = [
    r"application (?:for|to) (?:the )?(.+?)(?: at | position| role|$)",
    r"your application (?:for|to) (?:the )?(.+?)(?: at | position| role|$)",
    r"thank(?:s| you) for applying (?:for|to) (?:the )?(.+?)(?: at | position| role|$)",
    r"^(?:re:\s*)?(.+?) at .+",                    # "Software Engineer at Figma"
    r"interview (?:for|with) (.+?)(?: at | role|$)",
]


def _domain(addr: str) -> str:
    m = re.search(r"@([\w.-]+)", addr or "")
    return (m.group(1).lower() if m else "")


def _detect_source(from_addr: str, body: str) -> Optional[str]:
    dom = _domain(from_addr)
    for needle, label in ATS_DOMAINS.items():
        if needle in dom:
            return label
    if "linkedin.com" in (body or "").lower():
        return "LinkedIn"
    return None


def _detect_status(text: str) -> tuple[Optional[str], float]:
    t = text.lower()
    for status, patterns in STATUS_PATTERNS:
        for pat in patterns:
            if re.search(pat, t):
                return status, 0.85
    return None, 0.0


def _extract_company(from_name: str, from_addr: str, subject: str, body: str) -> Optional[str]:
    # 1. ATS emails put the company in the From name: "Figma via Greenhouse <no-reply@greenhouse.io>"
    if from_name:
        m = re.match(r"^(.+?)\s+(?:via|through)\s+(?:Greenhouse|Lever|Workday|Ashby)", from_name, re.I)
        if m:
            return m.group(1).strip()
    # 2. Subject pattern: "... at <Company>"
    m = re.search(r"\bat\s+([A-Z][\w &.\-]+?)(?:\s*[-–—|]|\s*$|\s+for\b)", subject)
    if m:
        return m.group(1).strip()
    # 3. From-name itself, if it doesn't look like a person
    if from_name and not re.search(r"\b(team|recruiting|talent|careers?|jobs?|hr|hiring)\b", from_name, re.I):
        # Looks like a company brand
        if from_name[:1].isupper() and " " not in from_name.strip().split(",")[0]:
            return from_name.strip()
    # 4. Fallback: domain (skip ATS domains)
    dom = _domain(from_addr)
    if dom and not any(ats in dom for ats in ATS_DOMAINS):
        # Strip common email subdomains
        parts = dom.split(".")
        if len(parts) >= 2:
            base = parts[-2]
            if base not in {"gmail", "google", "yahoo", "outlook", "hotmail"}:
                return base.capitalize()
    return None


def _extract_position(subject: str, body: str) -> Optional[str]:
    for pat in POSITION_SUBJECT_PATTERNS:
        m = re.search(pat, subject, re.I)
        if m:
            pos = m.group(1).strip(" -–—|:")
            if 3 <= len(pos) <= 120:
                return pos
    return None


JOB_KEYWORDS = (
    "application", "applied", "interview", "recruiter", "hiring",
    "candidacy", "offer", "onsite", "screen", "assessment", "candidate",
)


def looks_job_related(subject: str, body: str, from_addr: str) -> bool:
    blob = f"{subject}\n{body[:2000]}".lower()
    if any(k in blob for k in JOB_KEYWORDS):
        return True
    if any(ats in _domain(from_addr) for ats in ATS_DOMAINS):
        return True
    return False


def classify(subject: str, body: str, from_name: str, from_addr: str) -> ClassifyResult:
    subject = subject or ""
    body = body or ""
    if not looks_job_related(subject, body, from_addr):
        return ClassifyResult(is_job_related=False, reason="no job keywords")

    status, status_conf = _detect_status(f"{subject}\n{body[:3000]}")
    company = _extract_company(from_name, from_addr, subject, body)
    position = _extract_position(subject, body)
    source = _detect_source(from_addr, body)

    # Confidence: weighted combination
    conf = 0.0
    if status: conf += 0.4
    if company: conf += 0.35
    if position: conf += 0.25

    reason_bits = []
    if status: reason_bits.append(f"status={status}")
    if company: reason_bits.append(f"company={company}")
    if position: reason_bits.append(f"position={position[:40]}")
    if source: reason_bits.append(f"source={source}")

    return ClassifyResult(
        is_job_related=True,
        company=company,
        position=position,
        status=status,
        source=source,
        confidence=conf,
        reason=" ".join(reason_bits) or "no fields extracted",
    )
