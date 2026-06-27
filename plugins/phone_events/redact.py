"""Sensitive data redaction for phone events.

Applied to all notification and UI text before it reaches the LLM.
Defense against accidental exfiltration of OTPs, credit card numbers,
and other sensitive content that appears in notifications.
"""

from __future__ import annotations

import re
from typing import List, Tuple

# Patterns to redact, with replacement text.
_REDACTION_RULES: List[Tuple[re.Pattern, str]] = [
    # OTP / verification codes (4-8 digits, often preceded by keywords)
    (re.compile(
        r"(?:code|码|código|コード|код|pin|otp|verify|verification|인증)"
        r"[\s:：]*(\d{4,8})",
        re.IGNORECASE,
    ), r"[OTP_REDACTED]"),

    # Standalone 4-8 digit codes that look like OTPs (preceded by "is" or ":")
    (re.compile(r"(?:is|:)\s*(\d{4,8})\b"), r": [CODE_REDACTED]"),

    # Credit card numbers (13-19 digits, possibly with spaces/dashes)
    (re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{1,7}\b"), r"[CARD_REDACTED]"),

    # SSN-like patterns
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), r"[SSN_REDACTED]"),

    # Email addresses
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"), r"[EMAIL_REDACTED]"),

    # Phone numbers (international format)
    (re.compile(r"\+\d{1,3}[\s-]?\d{3,14}"), r"[PHONE_REDACTED]"),
]


def redact_sensitive(text: str, enable_otp: bool = True) -> str:
    """Apply redaction rules to text. Returns the redacted string."""
    if not text:
        return text
    result = text
    for pattern, replacement in _REDACTION_RULES:
        if not enable_otp and "OTP" in replacement:
            continue
        result = pattern.sub(replacement, result)
    return result


def truncate_notification_body(text: str, max_length: int = 200) -> str:
    """Truncate notification body to limit data exposure."""
    if not text or len(text) <= max_length:
        return text
    return text[:max_length] + "… [truncated]"
