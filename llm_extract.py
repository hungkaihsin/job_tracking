"""
Local LLM-based extraction via Ollama for emails the heuristic classifier
couldn't fully parse. Used as a fallback only — never replaces high-confidence
heuristic results.

Privacy: all inference is local. No data leaves the machine.
Cost: zero ($0). Performance: ~2-5s per email on M-series Macs with a 3B model.

Setup:
    brew install ollama          # or download from https://ollama.com
    ollama serve                  # macOS auto-starts; this is just to confirm
    ollama pull llama3.2:3b       # ~2GB download, one-time
    # In .env, set:
    USE_LLM_EXTRACTION=1
    OLLAMA_MODEL=llama3.2:3b      # optional; this is the default
"""
import logging
import os
import re
import time
import requests
from typing import Optional

log = logging.getLogger("jobbot.llm")

# Ollama (local fallback)
OLLAMA_BASE = "http://localhost:11434"
DEFAULT_MODEL = "llama3.2:3b"

# Gemini (cloud, preferred when API key is set — much higher accuracy)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Rate-limit Gemini to stay under the free-tier RPM cap.
# Flash:      10 RPM /  250 RPD
# Flash-lite: 15 RPM / 1000 RPD  <-- recommended; 4x daily headroom
_GEMINI_RPM_CAP = 13  # leave 2 RPM headroom for retry attempts
_GEMINI_CALL_TIMES: list = []


def _rate_limit_gemini():
    """Block until we can safely make another Gemini call without 429-ing."""
    now = time.time()
    # Drop timestamps older than 60s
    _GEMINI_CALL_TIMES[:] = [t for t in _GEMINI_CALL_TIMES if now - t < 60]
    if len(_GEMINI_CALL_TIMES) >= _GEMINI_RPM_CAP:
        oldest = _GEMINI_CALL_TIMES[0]
        wait = 60 - (now - oldest) + 0.5
        if wait > 0:
            log.info("Gemini RPM pacing: sleeping %.1fs", wait)
            time.sleep(wait)
    _GEMINI_CALL_TIMES.append(time.time())


def is_ollama_available(timeout: float = 2.0) -> bool:
    """Quick health check — used at startup to fail loud if LLM is enabled but Ollama isn't running."""
    try:
        r = requests.get(f"{OLLAMA_BASE}/api/tags", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _generate(prompt: str, model: str, timeout: float = 30.0) -> Optional[str]:
    try:
        r = requests.post(
            f"{OLLAMA_BASE}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.1,        # near-deterministic for extraction
                    "num_predict": 60,         # cap output length
                    "top_p": 0.9,
                },
            },
            timeout=timeout,
        )
        if r.status_code != 200:
            log.warning("Ollama returned %d: %s", r.status_code, r.text[:200])
            return None
        return r.json().get("response", "").strip()
    except requests.Timeout:
        log.warning("Ollama timeout after %ss", timeout)
        return None
    except Exception as e:
        log.warning("Ollama error: %s", e)
        return None


def _clean_llm_output(s: str) -> Optional[str]:
    """Normalize LLM response into a tidy position string, or None if junk."""
    if not s:
        return None
    # Take only the first line — sometimes models add explanations
    s = s.split("\n")[0].strip()
    # Strip common surrounding chars
    s = s.strip(" \"'`*-•—–.:")
    # Strip leading "Position:" / "Title:" if model echoes the label
    s = re.sub(r"^(position|role|job title|title)\s*[:.\-]\s*", "", s, flags=re.I)
    # Reject empty / "unknown" / too-long
    if not s or s.upper() in ("UNKNOWN", "N/A", "NONE", "NULL"):
        return None
    if len(s) < 3 or len(s) > 80:
        return None
    # Reject obvious noise (model refusing or hedging)
    if re.match(r"^(i\s|sorry|the email|based on|here|this email)", s, re.I):
        return None
    return s


def _build_prompt(subject: str, body: str, company: Optional[str]) -> str:
    body_excerpt = (body or "")[:2500]
    return f"""You extract structured data from job-application emails.

Subject: {subject or "(none)"}
Company: {company or "(unknown)"}

Email body:
{body_excerpt}

---
Task: Find the SPECIFIC job title/role mentioned in this email.

Rules:
- Output the role title ONLY, with no other text.
- Examples of valid output: "Software Engineer Intern", "Data Scientist II", "ML Research Engineer"
- Do NOT include the company name, location, or job ID.
- If the email doesn't mention a specific role (e.g. just a generic "thank you for applying" with no title), output exactly: UNKNOWN
- Maximum 80 characters.

Role title:"""


def _extract_with_gemini(subject: str, body: str, company: Optional[str], model: str) -> Optional[str]:
    """Call Gemini API with rate limiting and 429 backoff retry."""
    url = f"{GEMINI_BASE}/models/{model}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": _build_prompt(subject, body, company)}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 60,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    for attempt in range(3):
        _rate_limit_gemini()
        try:
            r = requests.post(url, json=payload, timeout=20)
        except requests.Timeout:
            log.warning("Gemini timeout (attempt %d)", attempt + 1)
            continue
        except Exception as e:
            log.warning("Gemini error: %s", e)
            return None

        if r.status_code == 200:
            data = r.json()
            candidates = data.get("candidates", [])
            if not candidates:
                return None
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts).strip()
            return text or None

        if r.status_code == 429:
            # Quota / rate limit. Pause and retry. The pacing usually prevents
            # this, but bursts of pre-warmed timestamps can race.
            wait = 15 * (attempt + 1)  # 15s, 30s, 45s
            log.warning("Gemini 429 (rate limit), backing off %ds (attempt %d/3)", wait, attempt + 1)
            time.sleep(wait)
            continue

        # Other non-success status — don't retry.
        log.warning("Gemini returned %d: %s", r.status_code, r.text[:200])
        return None

    log.warning("Gemini exhausted all retries; returning None")
    return None


def extract_position(
    subject: str,
    body: str,
    company: Optional[str] = None,
    model: str = DEFAULT_MODEL,
) -> Optional[str]:
    """Extract job title from an email using LLM.

    Provider selection:
    - If GEMINI_API_KEY is set: Gemini ONLY (no Ollama fallback). Cleaner — avoids
      404s when the local model has been removed; if Gemini fails, position stays
      Unknown and the bot moves on.
    - Otherwise: Ollama only.
    """
    if not body and not subject:
        return None

    if GEMINI_API_KEY:
        raw = _extract_with_gemini(subject, body, company, GEMINI_MODEL)
    else:
        raw = _generate(_build_prompt(subject, body, company), model)

    return _clean_llm_output(raw)
