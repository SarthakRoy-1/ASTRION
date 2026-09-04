"""Password hashing.

Uses `hashlib.scrypt` — a memory-hard KDF from the standard library, and one
of the algorithms OWASP names as acceptable for password storage. Nothing here
is invented: scrypt is RFC 7914, and this module only chooses parameters,
generates salt, and encodes the result.

The stored form is self-describing:

    scrypt$<n>$<r>$<p>$<salt-b64>$<hash-b64>

Carrying the parameters with the hash is what makes them changeable. A hash
written under weaker parameters still verifies, and `needs_rehash` tells the
caller to re-derive it at the next successful login, so a parameter increase
rolls forward without a migration or a forced reset.

Two properties this module is responsible for:

- **Verification is constant-time.** `hmac.compare_digest`, never `==`, so a
  timing signal cannot leak how much of a hash matched.
- **A wrong password and a malformed record are indistinguishable to the
  caller.** Both return False. A corrupt row must not raise where a wrong
  password returns, because the difference would be observable.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

#: Parameters. n=2**15 with r=8 puts one derivation at roughly 32 MB and a few
#: tens of milliseconds on ordinary hardware — costly enough to make offline
#: cracking expensive, cheap enough to sit in a login request.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32

#: scrypt's memory ceiling must be raised above the default 32 MB or a
#: derivation at these parameters raises instead of running.
_MAXMEM = 128 * SCRYPT_R * SCRYPT_N * 2

ALGORITHM = "scrypt"

#: Minimums enforced at registration and at every password change. Length is
#: the control that matters; composition rules push users toward predictable
#: substitutions and are deliberately not imposed.
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024


class PasswordError(ValueError):
    """A password was rejected before it was ever hashed."""


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(raw: str) -> bytes:
    return base64.b64decode(raw.encode("ascii"))


def validate_password(password: str) -> None:
    """Reject a password that cannot be stored safely. Raises `PasswordError`.

    The upper bound is a denial-of-service control, not a policy: scrypt
    happily derives from a megabyte of input, and an unbounded password field
    is an unbounded amount of work an unauthenticated caller can request.
    """
    if not isinstance(password, str):
        raise PasswordError("Password must be text.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordError(
            f"Password must be at most {MAX_PASSWORD_LENGTH} characters."
        )


def hash_password(
    password: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P
) -> str:
    """Derive a storable hash with a fresh random salt."""
    validate_password(password)
    salt = secrets.token_bytes(SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=KEY_BYTES,
        maxmem=128 * r * n * 2,
    )
    return f"{ALGORITHM}${n}${r}${p}${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, encoded: str) -> bool:
    """Check a password against a stored hash. False for anything unusable.

    Never raises on a malformed or unknown-algorithm record: a caller must not
    be able to tell a corrupt row from a wrong password.
    """
    if not isinstance(password, str) or not isinstance(encoded, str):
        return False
    if len(password) > MAX_PASSWORD_LENGTH:
        return False
    try:
        algorithm, n_raw, r_raw, p_raw, salt_raw, hash_raw = encoded.split("$")
        if algorithm != ALGORITHM:
            return False
        n, r, p = int(n_raw), int(r_raw), int(p_raw)
        salt = _unb64(salt_raw)
        expected = _unb64(hash_raw)
    except (ValueError, TypeError):
        return False

    try:
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=128 * r * n * 2,
        )
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(derived, expected)


def needs_rehash(encoded: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R) -> bool:
    """True when a stored hash was derived under weaker parameters than current.

    Called after a successful login so a parameter increase rolls forward on
    its own, one user at a time, without a migration or a forced reset.
    """
    try:
        algorithm, n_raw, r_raw, _p, _salt, _hash = encoded.split("$")
    except ValueError:
        return True
    if algorithm != ALGORITHM:
        return True
    try:
        return int(n_raw) < n or int(r_raw) < r
    except ValueError:
        return True


#: A hash of a password nobody holds, used to equalise the timing of a login
#: against a non-existent account. Derived once at import.
DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


def waste_time() -> None:
    """Burn the same work a real verification costs.

    Called on the login path when no such user exists, so "unknown account"
    and "wrong password" take the same time and neither can be distinguished
    by measurement. Without it, account enumeration is a stopwatch away.
    """
    verify_password("not-the-password", DUMMY_HASH)
