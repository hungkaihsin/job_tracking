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

# ---------- Company extraction from subject (most reliable signal) ----------
# Order matters: more specific patterns first.
COMPANY_SUBJECT_PATTERNS = [
    # "Thank you for applying to/at Foo" / "Thanks for Applying to Foo!"
    r"thank(?:s| you) for (?:applying|your application)\b[^,.\n]*?\b(?:to|at|with)\s+([A-Z][\w &./\-]+?)(?:\s*[-–—|!?,]|\s*$)",
    # "Thank you for your interest in Foo"
    r"thank(?:s| you) for your interest in\s+([A-Z][\w &./\-]+?)(?:\s*[-–—|!?,]|\s*$)",
    # "Your application to/for/at Foo" / "Your Foo Application"
    r"\byour application (?:to|for|at|with)\s+([A-Z][\w &./\-]+?)(?:\s*[-–—|!?,]|\s*$)",
    r"\byour\s+([A-Z][\w &./\-]+?)\s+(?:Careers\s+)?Application\b",
    # "Application received - Foo" / "Application Confirmation: Foo"
    r"application (?:received|confirmation|status)\b[\s:.\-—]+([A-Z][\w &./\-]+?)(?:\s*[-–—|!?,]|\s*$)",
    # "Regarding your application to/at Foo"
    r"regarding (?:your )?(?:application|candidacy|.+? application)\b[^.]*?\b(?:to|at|with|for)\s+([A-Z][\w &./\-]+?)(?:\s*[-–—|!?,]|\s*$)",
    # "Update on your application at Foo"
    r"update on your application (?:to|at|with|for)\s+([A-Z][\w &./\-]+?)(?:\s*[-–—|!?,]|\s*$)",
    # "<Foo> - thanks for applying" or "<Foo>: ..." (company at start)
    r"^([A-Z][\w &./\-]+?)\s*[-–—:|]+\s*(?:thank|application|your application)",
]

# ---------- Position extraction patterns (subject is sometimes reliable) ----------
# These ONLY fire when wording strongly implies a role title is present.
POSITION_SUBJECT_PATTERNS = [
    # "Application for the position of Foo" / "for the role of Foo"
    r"(?:application )?for the (?:position|role) of\s+(.+?)(?:\s+at\s|\s*[-–—|]|\s*$)",
    r"applying for (?:the )?(.+?)(?:\s+at\s|\s*[-–—|]|\s+position\b|\s+role\b|\s*$)",
    # "Foo Engineer at Bar" — leading text before "at" looks like position
    r"^(?:re:\s*)?([A-Z][^|–—\-]+?\b(?:Engineer|Developer|Analyst|Scientist|Designer|Manager|Intern)\b[^|–—\-]*?)\s+at\s+",
    # "Interview for Foo Role"
    r"interview (?:for|with) (?:the )?(.+?)(?:\s+at\s|\s+role\b|\s*[-–—|]|\s*$)",
]

# Body patterns — many ATS emails repeat the role in the body even when subject is generic.
POSITION_BODY_PATTERNS = [
    r"\bfor the (?:position|role) of\s+([^\n.,;]+?)(?:\s+at\s|\s+with\s|\s*[.,;\n])",
    r"\bfor the\s+([^\n.,;]+?)\s+(?:position|role|opportunity)\b",
    r"\bapplying for (?:the )?([^\n.,;]+?)(?:\s+at\s|\s+role\b|\s+position\b|\s*[.,;\n])",
    r"\byour application for (?:the )?([^\n.,;]+?)(?:\s+at\s|\s+role\b|\s+position\b|\s*[.,;\n])",
    r"\bregarding your application for\s+([^\n.,;]+?)(?:\s+at\s|\s*[.,;\n])",
    # "Position: Software Engineer" / "Role - Software Engineer"
    r"\b(?:Position|Role|Job Title|Title)\s*[:\-]\s*([^\n]+?)(?:\n|$)",
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


def _clean_company(s: str) -> str:
    """Tidy up extracted company strings."""
    s = s.strip(" -–—|:!?,.\"'")
    # Strip leading prepositions that slipped into the capture (case-insensitive
    # patterns can match "with Esri" / "to Stripe" — drop the prep).
    s = re.sub(r"^(with|to|at|for|in|the|a|an)\s+", "", s, flags=re.I)
    # Drop trailing role-ish suffixes
    s = re.sub(r"\s+(Careers|Recruiting|Talent|Hiring|Team)$", "", s, flags=re.I)
    return s.strip(" -–—|:!?,.\"'")


def _extract_company(from_name: str, from_addr: str, subject: str, body: str) -> Optional[str]:
    dom = _domain(from_addr)
    is_ats = any(ats in dom for ats in (
        "greenhouse", "lever", "workday", "ashby", "smartrecruiters",
        "icims", "jobvite", "myworkdayjobs", "paradox", "phenompeople",
    ))

    # 1. PRIMARY: extract company from subject — works regardless of sender domain.
    for pat in COMPANY_SUBJECT_PATTERNS:
        m = re.search(pat, subject, re.I)
        if m:
            cand = _clean_company(m.group(1))
            if 2 <= len(cand) <= 60 and not cand.lower().startswith(("application", "thank")):
                return cand

    # 2. ATS "via" pattern: "Figma via Greenhouse <no-reply@greenhouse.io>"
    if from_name:
        m = re.match(r"^(.+?)\s+(?:via|through)\s+(?:Greenhouse|Lever|Workday|Ashby)", from_name, re.I)
        if m:
            return _clean_company(m.group(1))

    # 3. For ATS senders, the From-NAME usually IS the company ("Stripe", "HeyGen Recruiting").
    if is_ats and from_name:
        cleaned = _clean_company(from_name)
        # Skip generic ATS labels
        if cleaned and not re.search(r"\b(no.?reply|do.?not.?reply|notification)\b", cleaned, re.I):
            return cleaned

    # 4. Body lookup: "Thank you for applying to <Company>" in first 1500 chars
    body_head = body[:1500] if body else ""
    for pat in COMPANY_SUBJECT_PATTERNS:
        m = re.search(pat, body_head, re.I)
        if m:
            cand = _clean_company(m.group(1))
            if 2 <= len(cand) <= 60 and not cand.lower().startswith(("application", "thank")):
                return cand

    # 5. Subject "... at <Company>" fallback
    m = re.search(r"\bat\s+([A-Z][\w &./\-]+?)(?:\s*[-–—|!?,.]|\s*$|\s+for\b)", subject)
    if m:
        return _clean_company(m.group(1))

    # 6. From-name when it looks like a brand (and isn't an ATS we already handled)
    if from_name and not is_ats:
        if not re.search(r"\b(team|recruiting|talent|careers?|jobs?|hr|hiring|no.?reply)\b", from_name, re.I):
            if from_name[:1].isupper():
                return _clean_company(from_name.split(",")[0])

    # 7. Last resort: company domain (skip ATS / mail providers / known noisy stems)
    BAD_DOMAIN_STEMS = {
        "gmail", "google", "yahoo", "outlook", "hotmail",
        "greenhouse-mail", "make", "send", "mail", "email", "notify", "notification",
    }
    if dom and not is_ats:
        parts = dom.split(".")
        if len(parts) >= 2:
            base = parts[-2]
            if base.lower() not in BAD_DOMAIN_STEMS:
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


# Multi-word phrases that strongly indicate a real application/interview email.
# Single keywords ("interview", "offer", "application") pull in too much noise.
JOB_PHRASES = (
    # Application confirmations
    "thank you for applying",
    "thanks for applying",
    "we have received your application",
    "we received your application",
    "your application has been received",
    "application has been submitted",
    "application confirmation",
    "your application to",
    "your application for",
    "regarding your application",
    "regarding your candidacy",
    "thank you for your application",
    "thank you for your interest in joining",
    "thank you for your interest in our",
    # Interviews
    "phone screen",
    "phone interview",
    "technical screen",
    "technical interview",
    "schedule an interview",
    "schedule a call",
    "schedule a phone",
    "recruiter screen",
    "recruiter call",
    "onsite interview",
    "on-site interview",
    "final round",
    "next round",
    "interview invitation",
    "interview request",
    # Offers / decisions
    "we are pleased to offer",
    "we are excited to offer",
    "we'd like to offer you",
    "we would like to offer",
    "extending an offer",
    "offer letter",
    "decided to move forward",
    "move forward with other candidates",
    "not moving forward",
    "regret to inform",
    "no longer being considered",
    "after careful consideration",
    "after careful review",
    "decision on your application",
    "update on your application",
    "your application status",
    "not selected for this",
    # Assessments
    "online assessment",
    "coding assessment",
    "coding challenge",
    "take-home assignment",
    "take home assignment",
    "hackerrank invite",
    "hackerrank assessment",
)

# Sender domains that are basically NEVER real job emails.
NOISE_DOMAINS = (
    "medium.com", "coursera.org", "udemy.com", "edx.org", "pluralsight.com",
    "harpercollins.com", "newsletter.", "marketing.", "promo.", "deals.",
)

# ATS domains that should always pass the filter (real recruiting infra).
STRONG_ATS = (
    "greenhouse.io", "lever.co", "myworkday.com", "myworkdayjobs.com",
    "ashbyhq.com", "smartrecruiters.com", "icims.com", "jobvite.com",
)


def looks_job_related(subject: str, body: str, from_addr: str) -> bool:
    dom = _domain(from_addr)
    if any(noise in dom for noise in NOISE_DOMAINS):
        return False
    # ATS senders almost always mean it's a real job email.
    if any(ats in dom for ats in STRONG_ATS):
        return True
    # Otherwise require a specific phrase match.
    blob = f"{subject}\n{body[:3000]}".lower()
    return any(p in blob for p in JOB_PHRASES)


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
