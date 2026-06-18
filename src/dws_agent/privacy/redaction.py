"""Regex + Shannon-entropy based secret/PII detection and redaction.

Replaces detected secrets and PII with category placeholders and reports the
taint level implied by the hits. Pure stdlib. No network, no LLM.

Detection strategy (deterministic, conservative):
  1. Named regex patterns for well-known secret/PII shapes (private keys,
     AKSK, bearer/JWT tokens, connection strings, emails, internal hosts).
  2. A generic high-entropy scan that flags long opaque tokens whose Shannon
     entropy exceeds ENTROPY_THRESHOLD (catches secrets without a fixed shape).

Each hit carries a category and the taint it implies. The overall result
exposes ``max_taint`` so callers can propagate it (see privacy.taint).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, List

from .taint import max_taint as _max_taint

# Generic-token entropy gate. Empirically ~4.0+ bits/char marks opaque
# secrets (base64/hex blobs) vs. natural-language words (~3.0-3.5).
ENTROPY_THRESHOLD = 4.0

# Minimum length before a token is even considered for the entropy scan;
# short high-entropy strings are too noisy to flag reliably.
_MIN_ENTROPY_TOKEN_LEN = 20


@dataclass
class Hit:
    """A single detected secret/PII occurrence.

    Attributes:
        category: which detector fired (e.g. 'private_key', 'email').
        start/end: span in the original text.
        taint: taint label implied by this category.
        placeholder: text substituted in the redacted output.
    """

    category: str
    start: int
    end: int
    taint: str
    placeholder: str


@dataclass
class RedactResult:
    """Result of :func:`redact`.

    Attributes:
        text: the redacted text (secrets replaced by placeholders).
        hits: list of :class:`Hit` describing what was found.
        max_taint: strictest taint across all hits ('CLEAN' if none).
    """

    text: str
    hits: List[Hit] = field(default_factory=list)
    max_taint: str = "CLEAN"


# Category -> taint level. Secrets are SENSITIVE; PII / internal topology is
# INTERNAL (org-internal but not a credential).
_CATEGORY_TAINT = {
    "private_key": "SENSITIVE",
    "aksk": "SENSITIVE",
    "bearer_token": "SENSITIVE",
    "jwt": "SENSITIVE",
    "connection_string": "SENSITIVE",
    "high_entropy": "SENSITIVE",
    "email": "INTERNAL",
    "internal_host": "INTERNAL",
    "phone": "INTERNAL",
}

# Named patterns. Order matters: more specific/structural patterns first so
# their spans win over the generic entropy scan.
PATTERNS: Dict[str, re.Pattern] = {
    # PEM private key blocks (RSA/EC/OPENSSH/etc.).
    "private_key": re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"
        r".*?-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----",
        re.DOTALL,
    ),
    # Cloud access-key / secret-key style identifiers (AK/SK, AWS, Aliyun LTAI).
    "aksk": re.compile(
        r"\b(?:AKIA[0-9A-Z]{16}|LTAI[0-9A-Za-z]{12,30}|AKID[0-9A-Za-z]{16,40})\b"
    ),
    # JWTs: three base64url segments separated by dots.
    "jwt": re.compile(
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
    ),
    # Authorization bearer tokens.
    "bearer_token": re.compile(
        r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]{12,}\b"
    ),
    # DB / service connection strings with embedded credentials.
    "connection_string": re.compile(
        r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:@/]+:[^\s:@/]+@[^\s/]+",
    ),
    # Phone numbers: Chinese mobile (11 digits starting 1[3-9], optional
    # separators / +86 country code) and generic separated digit runs.
    "phone": re.compile(
        r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d(?:[-\s]?\d){8}(?!\d)"
    ),
    # Emails.
    "email": re.compile(
        r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
    ),
    # Internal hostnames / corp domains and RFC1918-ish private hosts.
    "internal_host": re.compile(
        r"\b(?:[a-zA-Z0-9\-]+\.)+(?:internal|corp|local|intranet|lan)\b"
        r"|\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){2,3}\b"
    ),
}

# Token splitter for the generic entropy scan.
_TOKEN_RE = re.compile(r"[^\s\"'<>(){}\[\],;]+")


def shannon_entropy(s: str) -> float:
    """Return the Shannon entropy (bits per character) of *s*.

    Empty strings return 0.0. Higher values indicate more randomness, which
    is characteristic of opaque secrets rather than natural-language text.
    """
    if not s:
        return 0.0
    counts: Dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    entropy = 0.0
    for c in counts.values():
        p = c / n
        entropy -= p * math.log2(p)
    return entropy


def redact(text: str) -> RedactResult:
    """Detect and redact secrets/PII in *text*.

    Runs named regex detectors then a generic high-entropy token scan over
    regions not already covered. Overlapping spans are resolved so each
    character is redacted at most once (named patterns take precedence).

    Returns a :class:`RedactResult` with the redacted text, the list of hits,
    and the strictest taint implied by those hits.
    """
    if not isinstance(text, str) or not text:
        return RedactResult(text=text if isinstance(text, str) else "", hits=[], max_taint="CLEAN")

    spans: List[Hit] = []
    # Track covered ranges to avoid double-flagging the same characters.
    covered: List[tuple] = []

    def _overlaps(start: int, end: int) -> bool:
        for cs, ce in covered:
            if start < ce and end > cs:
                return True
        return False

    # 1. Named patterns.
    for category, pattern in PATTERNS.items():
        taint = _CATEGORY_TAINT.get(category, "INTERNAL")
        for m in pattern.finditer(text):
            start, end = m.start(), m.end()
            if _overlaps(start, end):
                continue
            spans.append(
                Hit(
                    category=category,
                    start=start,
                    end=end,
                    taint=taint,
                    placeholder=f"[REDACTED:{category.upper()}]",
                )
            )
            covered.append((start, end))

    # 2. Generic high-entropy scan over uncovered tokens.
    for m in _TOKEN_RE.finditer(text):
        token = m.group(0)
        start, end = m.start(), m.end()
        if len(token) < _MIN_ENTROPY_TOKEN_LEN:
            continue
        if _overlaps(start, end):
            continue
        if shannon_entropy(token) >= ENTROPY_THRESHOLD:
            spans.append(
                Hit(
                    category="high_entropy",
                    start=start,
                    end=end,
                    taint=_CATEGORY_TAINT["high_entropy"],
                    placeholder="[REDACTED:HIGH_ENTROPY]",
                )
            )
            covered.append((start, end))

    if not spans:
        return RedactResult(text=text, hits=[], max_taint="CLEAN")

    # Build redacted text by replacing spans left-to-right.
    spans.sort(key=lambda h: h.start)
    out_parts: List[str] = []
    cursor = 0
    for hit in spans:
        out_parts.append(text[cursor:hit.start])
        out_parts.append(hit.placeholder)
        cursor = hit.end
    out_parts.append(text[cursor:])
    redacted = "".join(out_parts)

    overall = _max_taint(*[h.taint for h in spans])
    return RedactResult(text=redacted, hits=spans, max_taint=overall)
