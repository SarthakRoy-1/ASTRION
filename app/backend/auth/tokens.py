"""Opaque secret tokens: session tokens, verification links, reset links.

Every secret this system issues follows one shape, and the shape is the
security property:

    issue()   ->  (plaintext, sha256 digest)
                  the plaintext goes to the user, exactly once
                  the digest goes to the database

Nothing that can be replayed is ever stored. A database disclosure therefore
yields no usable session token and no usable reset link — the same reason
passwords are hashed, applied to the credentials the server itself hands out.

SHA-256 with no salt and no stretching is correct *here* and would be wrong
for a password: these tokens are 256 bits of `secrets` output, so there is no
guess to accelerate and no dictionary to precompute. Stretching them would buy
nothing and cost a KDF on every request.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

#: 32 bytes of CSPRNG output, url-safe encoded. Comfortably beyond any
#: brute-force reach, and short enough to sit in a cookie or a link.
TOKEN_BYTES = 32


def new_token() -> str:
    """A fresh, unguessable secret. Shown to its owner once and never stored."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """The storable digest of a token. This is what goes in the database."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(a: str, b: str) -> bool:
    """Constant-time comparison, for comparing digests without a timing leak."""
    return hmac.compare_digest(a, b)


def new_id(prefix: str) -> str:
    """A non-secret, collision-resistant identifier, e.g. `USR-9f2c...`.

    Random rather than sequential on purpose: a sequential id tells anyone who
    holds one how many exist and what the neighbouring ones are.
    """
    return f"{prefix}-{secrets.token_hex(8)}"
