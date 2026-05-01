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
    r"thank(?:s| you) for (?:applying|your application)\b[^,.\n]*?\b(?:to|at|with)\s+((?-i:[A-Z])[\w &./\-]+?)(?:\s*[-–—|!?,]|\s*$)",
    # "Thank you for your interest in Foo"
    r"thank(?:s| you) for your interest in\s+((?-i:[A-Z])[\w &./\-]+?)(?:\s*[-–—|!?,]|\s*$)",
    # "Your application to/for/at Foo" / "Your Foo Application"
    r"\byour application (?:to|for|at|with)\s+((?-i:[A-Z])[\w &./\-]+?)(?:\s*[-–—|!?,]|\s*$)",
    r"\byour\s+((?-i:[A-Z])[\w &./\-]+?)\s+(?:Careers\s+)?Application\b",
    # "Application received - Foo" / "Application Confirmation: Foo"
    r"application (?:received|confirmation|status)\b[\s:.\-—]+((?-i:[A-Z])[\w &./\-]+?)(?:\s*[-–—|!?,]|\s*$)",
    # "Regarding your application to/at Foo"
    r"regarding (?:your )?(?:application|candidacy|.+? application)\b[^.]*?\b(?:to|at|with|for)\s+((?-i:[A-Z])[\w &./\-]+?)(?:\s*[-–—|!?,]|\s*$)",
    # "Update on your application at Foo"
    r"update on your application (?:to|at|with|for)\s+((?-i:[A-Z])[\w &./\-]+?)(?:\s*[-–—|!?,]|\s*$)",
    # "<Foo> - thanks for applying" or "<Foo>: ..." (company at start)
    r"^((?-i:[A-Z])[\w &./\-]+?)\s*[-–—:|]+\s*(?:thank|application|your application)",
    # "<Company> Application <Update|Status|Received|Confirmation> ..." — company at start, no separator.
    r"^((?-i:[A-Z])[\w &./\-]+?)\s+Application\s+(?:Update|Status|Received|Confirmation|Submitted)\b",
    # "Thanks for Considering <Company>"
    r"thank(?:s| you) for considering\s+((?-i:[A-Z])[\w &./\-]+?)(?:\s*[-–—|!?,]|\s*$)",
    # "<Company> received your application"
    r"^((?-i:[A-Z])[\w &./\-]+?)\s+received your application\b",
]

# ---------- Position extraction patterns (subject is sometimes reliable) ----------
POSITION_SUBJECT_PATTERNS = [
    # "Application for the position of Foo" / "for the role of Foo"
    r"(?:application )?for the (?:position|role) of\s+(.{3,80}?)(?:\s+at\s|\s*[-–—|]|\s*$)",
    r"applying for (?:the )?(.{3,80}?)(?:\s+at\s|\s*[-–—|]|\s+position\b|\s+role\b|\s*$)",
    # "Foo Engineer at Bar" — leading text before "at" with a role keyword
    r"^(?:re:\s*)?((?-i:[A-Z])[^|–—\-]+?\b(?:Engineer|Developer|Analyst|Scientist|Designer|Manager|Intern|Researcher|Specialist|Consultant|Architect|Lead|Director)\b[^|–—\-]*?)\s+at\s+",
    # "Interview for Foo Role"
    r"interview (?:for|with) (?:the )?(.{3,80}?)(?:\s+at\s|\s+role\b|\s*[-–—|]|\s*$)",
]

# Body patterns — limited to 3-60 char captures to prevent greedy multi-sentence matches.
POSITION_BODY_PATTERNS = [
    r"\bfor the (?:position|role) of (?:the\s+)?([^\n.,;]{3,60}?)(?:\s*[.,;\n]|\s+at\s|\s+with\s|\s+team\b)",
    r"\bfor the\s+([^\n.,;]{3,60}?)\s+(?:position|role|opportunity)\b",
    r"\bapplying for (?:the )?([^\n.,;]{3,60}?)(?:\s+at\s|\s+role\b|\s+position\b|\s*[.,;\n])",
    r"\byour application for (?:the )?([^\n.,;]{3,60}?)(?:\s+at\s|\s+role\b|\s+position\b|\s*[.,;\n])",
    r"\bregarding your application for\s+([^\n.,;]{3,60}?)(?:\s+at\s|\s*[.,;\n])",
    # "Position: Software Engineer" / "Role - Software Engineer"
    r"\b(?:Position|Role|Job Title|Title)\s*[:\-]\s*([^\n]{3,60}?)(?:\n|$)",
]

# Combined patterns that capture BOTH company and position in one match — for
# common formats like "to <Company> - <Role>" / "<Role> at <Company>".
# Each tuple is (regex, "co_first" or "pos_first").
COMBINED_SUBJECT_PATTERNS = [
    # "...to/at/for/with <Company> - <Position>" e.g. "Application to Agilent - Data Scientist"
    (r"\b(?:to|at|for|with)\s+((?-i:[A-Z])[\w &.]{1,40}?)\s*[-–—]\s*((?-i:[A-Z])[\w &./\-]{2,80}?)(?:\s*[-–—|!?,]|\s*$)", "co_first"),
    # "<Company> - <Position>" with optional ID prefix
    # e.g. "Boston Scientific - Data Scientist I" / "Application Update - R_323393 Oliver Wyman - Summer Analyst"
    (r"\b((?-i:[A-Z])[\w &.]{1,40}?)\s*[-–—]\s*((?-i:[A-Z])[\w &./\-]{2,80}?\b(?:Engineer|Developer|Analyst|Scientist|Designer|Manager|Intern|Researcher|Specialist|Consultant|Architect|Lead|Director|Internship|Associate)\b[\w &./\-]*?)(?:\s*[-–—|!?,(]|\s*$)", "co_first"),
    # "<Company>! (<Position>)" — company with exclamation, role in parens
    (r"^((?-i:[A-Z])[\w &./\-]{1,40}?)!\s*\(((?-i:[A-Z])[^)]{2,80}?)\)", "co_first"),
    # "<Company> Application Update for <Position>" — Nordstrom-style
    (r"^((?-i:[A-Z])[\w &./\-]{1,40}?)\s+Application\s+(?:Update|Status)\s+for\s+((?-i:[A-Z])[\w &./\-]{2,80}?)(?:\s*[-–—|!?,]|\s*$)", "co_first"),
    # "Your Application for <Position> at <Company>"
    (r"\b(?:application|interest)\s+for\s+(?:the\s+)?((?-i:[A-Z])[\w &./\-]{2,80}?)\s+at\s+((?-i:[A-Z])[\w &./\-]{1,40}?)(?:\s*[-–—|!?,]|\s*$)", "pos_first"),
    # "<Position> at <Company>" e.g. "Software Engineer at Stripe"
    (r"^(?:re:\s*)?((?-i:[A-Z])[\w &./\-]{2,80}?\b(?:Engineer|Developer|Analyst|Scientist|Designer|Manager|Intern|Researcher|Specialist|Consultant|Architect|Lead|Director)\b[\w &./\-]*?)\s+at\s+((?-i:[A-Z])[\w &./\-]{1,40}?)(?:\s*[-–—|!?,]|\s*$)", "pos_first"),
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
    # Strip Workday/ATS-system PREFIXES on from_name: "Workday - MMC" -> "MMC",
    # "WorkdaySystem_DoNotReply" -> "" (will fail validation), "Workday@Cisco" -> "Cisco".
    s = re.sub(
        r"^(?:workday(?:system)?|workday_no_?reply|workday\s+notification|"
        r"system|donotreply|noreply)\s*[\s\-@_:]+",
        "", s, flags=re.I,
    )
    # If what remains starts with a job-ID-looking token (JR123, R_456), reject by emptying.
    if re.match(r"^(?:JR|R_?)\d{3,}\b", s):
        return ""
    # Strip trailing "@ <ATS>" suffixes that some ATSes append to From-name
    # (e.g. "Geosyntec Consultants, Inc. @ icims" -> "Geosyntec Consultants, Inc.").
    s = re.sub(
        r"\s*@\s*(icims|greenhouse|greenhouse-?mail|lever|workday|"
        r"ashby|ashbyhq|smartrecruiters|jobvite|paradox|phenom)\b.*$",
        "", s, flags=re.I,
    )
    # Strip "(via <ATS>)" parenthetical
    s = re.sub(r"\s*\((?:via|through)\s+\w+\)$", "", s, flags=re.I)
    # Strip trailing ATS / role-ish suffixes
    s = re.sub(
        r"\s+(Workday\s+Notification|Workday|Greenhouse|"
        r"Recruiting(?:\s+Team)?|Talent(?:\s+Team)?|Hiring(?:\s+Team)?|"
        r"Team|Notification|Notifications|Careers|HR|People\s+Team)$",
        "", s, flags=re.I,
    )
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
            if _is_valid_company(cand):
                return cand

    # 2. ATS "via" pattern: "Figma via Greenhouse <no-reply@greenhouse.io>"
    if from_name:
        m = re.match(r"^(.+?)\s+(?:via|through)\s+(?:Greenhouse|Lever|Workday|Ashby)", from_name, re.I)
        if m:
            return _clean_company(m.group(1))

    # 3. For ATS senders, the From-NAME usually IS the company ("Stripe", "HeyGen Recruiting").
    # But reject generic ATS labels like "Greenhouse-mail" or "Workday" — those mean
    # the ATS didn't put a company name in the field.
    GENERIC_ATS_NAMES = {
        "greenhouse", "greenhouse-mail", "greenhouse mail", "lever", "workday",
        "myworkday", "ashby", "ashbyhq", "smartrecruiters", "icims", "jobvite",
        "phenom", "phenompeople", "paradox", "no-reply", "no reply", "noreply",
    }
    if is_ats and from_name:
        cleaned = _clean_company(from_name)
        if (cleaned
            and cleaned.lower() not in GENERIC_ATS_NAMES
            and not re.search(r"\b(no.?reply|do.?not.?reply|notification)\b", cleaned, re.I)):
            return cleaned

    # 4. Body lookup: "Thank you for applying to <Company>" in first 1500 chars
    body_head = body[:1500] if body else ""
    for pat in COMPANY_SUBJECT_PATTERNS:
        m = re.search(pat, body_head, re.I)
        if m:
            cand = _clean_company(m.group(1))
            if _is_valid_company(cand):
                return cand

    # 5. Subject "... at <Company>" fallback
    m = re.search(r"\bat\s+((?-i:[A-Z])[\w &./\-]+?)(?:\s*[-–—|!?,.]|\s*$|\s+for\b)", subject)
    if m:
        return _clean_company(m.group(1))

    # 6. From-name when it looks like a brand. _clean_company strips trailing
    # "Recruiting"/"Talent"/etc. so "BCG Recruiting" -> "BCG" still works as long
    # as something is left after stripping.
    if from_name and not is_ats:
        cleaned = _clean_company(from_name.split(",")[0])
        # Reject if nothing meaningful is left, or if the result is itself
        # a generic noise word (e.g. from_name was just "Recruiting Team").
        if (cleaned
            and len(cleaned) >= 2
            and cleaned[:1].isupper()
            and not re.match(r"^(team|recruiting|talent|careers?|jobs?|hr|hiring|"
                             r"no.?reply|do.?not.?reply|notification|notifications)$",
                             cleaned, re.I)):
            return cleaned

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


def _clean_position(p: str) -> str:
    p = p.strip(" -–—|:!?,.\"'\n\t")
    # Strip "the " prefix
    p = re.sub(r"^(the|our|a|an)\s+", "", p, flags=re.I)
    return p.strip(" -–—|:!?,.\"'\n\t")


# Reject these as positions — subject leftovers, not real role titles.
POSITION_NOISE = re.compile(
    r"^(thank|thanks|your|application|interest|update|regarding|hello|hi|dear|congrat|"
    r"following|time|below|above|this|that|such|similar|given|specified|mentioned|"
    r"role of\b|position of\b|opportunity\b)",
    re.I,
)


def _is_valid_company(c: str) -> bool:
    """A captured company string must (a) actually start with an uppercase letter
    (the IGNORECASE flag in our patterns lets lowercase slip through `[A-Z]`),
    (b) be 2-60 chars, (c) not be a known noise phrase, (d) not contain role/position
    words (which would mean we accidentally captured a position description instead)."""
    if not c or len(c) < 2 or len(c) > 60:
        return False
    if not c[0].isupper():
        return False
    low = c.lower()
    if low.startswith(("application", "thank", "thanks", "the ", "this ", "that ",
                       "your ", "our ", "interest", "regarding", "following",
                       "time ", "role of", "position of")):
        return False
    # Reject captures that contain role/position words — those are position descriptions, not companies.
    if re.search(r"\b(position|role|opportunity|title|posting|requisition)\b", low):
        return False
    # Reject any role keyword anywhere in the capture (companies almost never contain these).
    if re.search(
        r"\b(engineer|developer|analyst|scientist|designer|intern|researcher|"
        r"architect|consultant)\b",
        low,
    ):
        return False
    return True


def _is_valid_position(p: str) -> bool:
    if not p or len(p) < 3 or len(p) > 80:
        return False
    if not p[0].isupper():
        return False
    if POSITION_NOISE.search(p):
        return False
    return True


def _try_combined_subject(subject: str) -> tuple[Optional[str], Optional[str]]:
    """Try to extract (company, position) together from common dash/at formats.

    Uses finditer so that if the first match has invalid groups (e.g. captured
    "your application to X" instead of just "X"), we keep trying later matches.
    """
    for pat, order in COMBINED_SUBJECT_PATTERNS:
        for m in re.finditer(pat, subject, re.I):
            if order == "co_first":
                co = _clean_company(m.group(1))
                pos = _clean_position(m.group(2))
            else:  # pos_first
                pos = _clean_position(m.group(1))
                co = _clean_company(m.group(2))
            if _is_valid_company(co) and _is_valid_position(pos):
                return co, pos
    return None, None


def _extract_position(subject: str, body: str) -> Optional[str]:
    # Try subject patterns first (most reliable when wording is precise)
    for pat in POSITION_SUBJECT_PATTERNS:
        m = re.search(pat, subject, re.I)
        if m:
            pos = _clean_position(m.group(1))
            if _is_valid_position(pos):
                return pos
    # Body patterns — check first 3000 chars
    body_head = body[:3000] if body else ""
    for pat in POSITION_BODY_PATTERNS:
        m = re.search(pat, body_head, re.I)
        if m:
            pos = _clean_position(m.group(1))
            if _is_valid_position(pos):
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

    # Try the combined dash/at subject pattern first — it's the most reliable when present
    # because it gives us both company AND position in a structured way.
    combined_co, combined_pos = _try_combined_subject(subject)
    company = combined_co or _extract_company(from_name, from_addr, subject, body)
    position = combined_pos or _extract_position(subject, body)
    source = _detect_source(from_addr, body)

    # Confidence: company is the dominant signal — without it, the row is meaningless.
    conf = 0.0
    if company: conf += 0.55
    if status: conf += 0.30
    if position: conf += 0.15

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
