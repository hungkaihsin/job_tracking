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
import re
import requests
from typing import Optional

log = logging.getLogger("jobbot.llm")

OLLAMA_BASE = "http://localhost:11434"
DEFAULT_MODEL = "llama3.2:3b"


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


def extract_position(
    subject: str,
    body: str,
    company: Optional[str] = None,
    model: str = DEFAULT_MODEL,
) -> Optional[str]:
    """Use Ollama to extract the job title/position from an email.

    Returns None if the LLM can't find one or Ollama is unavailable.
    """
    if not body and not subject:
        return None
    body_excerpt = (body or "")[:2500]
    prompt = f"""You extract structured data from job-application emails.

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
    raw = _generate(prompt, model)
    return _clean_llm_output(raw)
