"""Time-based one-time passwords (RFC 6238), for second-factor login.

This implements the published standard and nothing else. TOTP is HMAC-SHA1
over a counter derived from the clock — the whole algorithm is the handful of
lines in `_hotp` below, and writing it out is not designing a protocol; it is
following one, the same one Google Authenticator, 1Password and Aegis
implement. The alternative would be a dependency for thirty lines of stdlib
`hmac`.

Two defences beyond the bare algorithm, both required and both easy to omit:

- **Comparison is constant-time.** A digit-by-digit `==` on a 6-digit code
  leaks position-of-first-difference, which reduces a 10^6 search to about 60
  guesses.
- **A used code cannot be replayed.** The window that makes TOTP usable
  against clock skew also gives an attacker who observes a code up to ninety
  seconds to reuse it. `verify` reports *which* time-step matched so the
  caller can record it and refuse that step again — see
  `app/backend/auth/service.py`.
"""

from __future__ import annotations

import base64
import hmac
import secrets
import struct
import time
from urllib.parse import quote

#: RFC 6238 defaults, and what every mainstream authenticator app assumes.
TIME_STEP_SECONDS = 30
DIGITS = 6

#: How many steps either side of the present are accepted. One step each way
#: tolerates about ±30s of clock skew. Widening this widens the replay window
#: by the same amount, so it stays at the smallest usable value.
DEFAULT_WINDOW = 1

SECRET_BYTES = 20  # 160 bits, the size RFC 4226 specifies for HMAC-SHA1


def new_secret() -> str:
    """A fresh base32 TOTP secret, in the form authenticator apps expect."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii")


def _decode_secret(secret: str) -> bytes:
    padded = secret.strip().replace(" ", "").upper()
    padded += "=" * (-len(padded) % 8)
    return base64.b32decode(padded, casefold=True)


def _hotp(key: bytes, counter: int, digits: int = DIGITS) -> str:
    """RFC 4226 HOTP: HMAC-SHA1, dynamic truncation, modulo 10^digits."""
    digest = hmac.new(key, struct.pack(">Q", counter), "sha1").digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**digits)).zfill(digits)


def generate(secret: str, *, at: float | None = None, digits: int = DIGITS) -> str:
    """The code valid at `at` (default now). Used by tests and by enrolment."""
    counter = int((time.time() if at is None else at) // TIME_STEP_SECONDS)
    return _hotp(_decode_secret(secret), counter, digits)


def verify(
    secret: str,
    code: str,
    *,
    at: float | None = None,
    window: int = DEFAULT_WINDOW,
    last_used_step: int | None = None,
) -> int | None:
    """Check a code. Returns the matching time-step, or None.

    The time-step is returned rather than a bare boolean so the caller can
    persist it and pass it back as `last_used_step`, which is what makes a
    code single-use. Without that, any code stays valid for the whole
    acceptance window and an observed code can simply be replayed.
    """
    if not code:
        return None
    cleaned = code.strip().replace(" ", "").replace("-", "")
    if not cleaned.isdigit() or len(cleaned) != DIGITS:
        return None

    try:
        key = _decode_secret(secret)
    except (ValueError, TypeError):
        return None

    current = int((time.time() if at is None else at) // TIME_STEP_SECONDS)
    matched: int | None = None
    for offset in range(-window, window + 1):
        step = current + offset
        if last_used_step is not None and step <= last_used_step:
            # Already spent. Keep looping rather than returning: the loop must
            # take the same time whichever step matches.
            continue
        if hmac.compare_digest(_hotp(key, step), cleaned):
            matched = step
    return matched


def provisioning_uri(secret: str, *, account_name: str, issuer: str) -> str:
    """The `otpauth://` URI an authenticator app scans as a QR code.

    Built here rather than in a route so the parameters stay in one place and
    the secret is never assembled into a URL by string concatenation at a call
    site that might log it.
    """
    label = quote(f"{issuer}:{account_name}", safe="")
    return (
        f"otpauth://totp/{label}?secret={secret}"
        f"&issuer={quote(issuer, safe='')}"
        f"&algorithm=SHA1&digits={DIGITS}&period={TIME_STEP_SECONDS}"
    )
