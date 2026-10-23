"""A bounded excerpt of a check-in's report text, for the persisted state row.

The full `sync_state` report text is not kept. Each check-in row keeps this
excerpt so a later reader can see what the agent said beside the state it was
given (the per-agent timeline, reconstruction after the fact). It is the
agent's own report, not a verified claim: durable claims still belong in a
finding.

The excerpt is cut at a word boundary. A hard cut at a fixed width used to
split identifiers mid-token ("each waits on #21" for a PR number with more
digits), which made the retained text misleading rather than merely short.

Credentials are scrubbed by shape before storage. This is best effort: it
catches the common token formats and `name=value` pairs whose name ends in a
secret-looking word, not every secret a caller might paste. The row sits
behind the same auth as every other check-in record.

`response_text` has no length cap, and this runs on the check-in path, so only
a bounded window is ever scanned and every pattern's repetition is bounded: a
crafted report cannot make scrubbing slow. Nothing here raises.
"""

from __future__ import annotations

import os
import re
from typing import Optional

DEFAULT_EXCERPT_CHARS = 500
MAX_EXCERPT_CHARS = 2000
REDACTED = "[REDACTED]"

# Text scanned beyond the kept length, so a secret that starts inside the
# excerpt is seen whole before the cut (no recognised token is longer).
_SCAN_MARGIN = 512

_SECRET_PATTERNS = (
    # PEM / PGP private key blocks, whole (or to the end of the window).
    re.compile(
        r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY(?: BLOCK)?-----.*?"
        r"(?:-----END [A-Z ]{0,40}PRIVATE KEY(?: BLOCK)?-----|\Z)",
        re.S,
    ),
    # Provider token shapes.
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{20,255})"),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{16,255}"),
    re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{10,255}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,255}"),
    re.compile(r"\bxapp-[A-Za-z0-9\-]{10,255}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,255}"),
    re.compile(r"\bhf_[A-Za-z0-9]{30,255}"),
    re.compile(r"\bnpm_[A-Za-z0-9]{36}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,2000}\.[A-Za-z0-9_\-]{10,2000}\.[A-Za-z0-9_\-]{10,2000}"),
    # Credentials inside a URL: scheme://user:pass@host (the password may hold '@').
    re.compile(r"(?<=://)[^/\s:@]{1,128}:[^/\s]{1,256}(?=@[^/\s@]{1,256})"),
)
# `Bearer <token>` in any case; `Basic <base64>` only capitalised and with a
# digit or base64 padding, so "basic authentication was the cause" survives.
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/\-]{12,1024}=*")
_BASIC = re.compile(r"\b(Basic\s+)(?=[A-Za-z0-9+/]{0,1023}(?:[0-9]|=))[A-Za-z0-9+/]{12,1024}=*")
# `name=value`, `name: value`, `"name": "value"` where the NAME ENDS in a
# secret-looking word, so `max_tokens=200000`, `test_token_refresh.py::x` and
# `password_reset.py:12` are left alone.
_SECRET_NAME = (
    r"[\w.\-]{0,64}?(?:token|secret|password|passwd|credentials?"
    r"|(?:api|private|access|secret|client|signing)[_-]?key)"
)
# Values are matched possessively to their end, so a long value is redacted
# whole rather than leaving its tail; the scanned window bounds the cost.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(" + _SECRET_NAME + r"[\"']?\s{0,4}[=:]\s{0,4})"
    r"(?:\"([^\"\n]++)\"|'([^'\n]++)'|([^\s\"',;)}\]]++))"
)
# Control characters (keeping tab/newline/CR for the whitespace collapse) and
# lone surrogates: Postgres jsonb rejects \u0000 and unpaired surrogates, and
# one bad character would otherwise lose the whole state row.
_UNSTORABLE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")
_WHITESPACE = re.compile(r"\s+")


def excerpt_chars() -> int:
    """Characters kept per check-in (UNITARES_CHECKIN_EXCERPT_CHARS).

    Default 500, the length auto-recorded outcomes already keep; 0 turns
    retention off; values above 2000 are capped.
    """
    raw = os.getenv("UNITARES_CHECKIN_EXCERPT_CHARS", "").strip()
    if not raw:
        return DEFAULT_EXCERPT_CHARS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_EXCERPT_CHARS
    return max(0, min(MAX_EXCERPT_CHARS, value))


def _looks_secret(value: str, *, assignment: bool) -> bool:
    # `name=value` and quoted values are deliberate assignments. After a bare
    # colon the value may be prose ("The token: refreshed correctly"), so it
    # must also carry a digit or be long.
    if len(value) < 8:
        return False
    return assignment or any(c.isdigit() for c in value) or len(value) >= 20


def _redact_assignment(match: re.Match) -> str:
    value = match.group(2) or match.group(3) or match.group(4) or ""
    quoted = match.group(4) is None
    if not _looks_secret(value, assignment=quoted or "=" in match.group(1)):
        return match.group(0)
    if match.group(2) is not None:
        return f'{match.group(1)}"{REDACTED}"'
    if match.group(3) is not None:
        return f"{match.group(1)}'{REDACTED}'"
    return match.group(1) + REDACTED


def scrub_credentials(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = _BEARER.sub(lambda m: m.group(1) + REDACTED, text)
    text = _BASIC.sub(lambda m: m.group(1) + REDACTED, text)
    return _SECRET_ASSIGNMENT.sub(_redact_assignment, text)


def make_report_excerpt(text: Optional[str], limit: Optional[int] = None) -> Optional[str]:
    """Scrubbed, whitespace-collapsed excerpt of at most `limit` characters.

    None when there is no text, retention is off, or anything goes wrong. A
    longer text is cut at the last whitespace that fits and ends with "…"; a
    single word longer than the limit is cut at the limit.
    """
    try:
        if not text or not isinstance(text, str):
            return None
        limit = excerpt_chars() if limit is None else limit
        if limit < 2:
            return None
        window_end = limit + _SCAN_MARGIN
        window = _UNSTORABLE.sub("", text[:window_end])
        collapsed = _WHITESPACE.sub(" ", scrub_credentials(window)).strip()
        if not collapsed:
            return None
        if len(collapsed) <= limit and len(text) <= window_end:
            return collapsed
        room = limit - 1  # leave space for the ellipsis
        cut = collapsed.rfind(" ", 0, room + 1)
        head = collapsed[:cut] if cut > 0 else collapsed[:room]
        return head.rstrip() + "…"
    except Exception:
        return None
