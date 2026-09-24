"""Email verification by one-time code.

A *verification* is a browser's claim to be proving one address, held as an
HttpOnly cookie in exactly the way a session is (the database stores only the
SHA-256 of the cookie value). Codes are issued inside it, one at a time; the
newest code is the only one that works.

The properties this module holds, and where each one comes from:

- **Cryptographically generated.** `secrets.randbelow`, six digits.
- **Never stored in plaintext.** HMAC-SHA256 keyed by a per-row random salt,
  over the verification id and the code. A stored hash is bound to its own
  verification and useless for any other.
- **Short-lived.** `OTP_TTL_MINUTES` (default 10); expiry is checked on use.
- **Single use.** Consumed under a guarded `UPDATE`, so two simultaneous
  submissions of the right code cannot both succeed.
- **Superseded on resend.** Issuing a code retires every earlier live code for
  the same verification *and the same address*, so an older email is dead.
- **Bounded guessing.** `OTP_MAX_ATTEMPTS` (default 5) wrong guesses burn a
  code, and at most `OTP_MAX_SENDS_PER_WINDOW` codes (default 5) go to one
  address per `OTP_SEND_WINDOW_MINUTES` (default 60). That caps an attacker at
  25 guesses an hour against a space of a million — and the per-IP rate limit
  on `/api/auth/` sits in front of all of it.
- **No existence oracle.** The verify endpoints take no address: a
  verification is only issued to someone who created the account or just
  presented its correct password. Registering a taken address yields a
  *decoy* verification that behaves identically — codes, expiry, attempts,
  cooldowns — and can never succeed.

Nothing here sends mail or touches a cookie. It decides; the route acts.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from app.backend.auth import repository as repo
from app.backend.auth.tokens import hash_token, new_id, new_token

#: Proving the address of an account that already exists (registration, or a
#: password sign-in to an account that was never verified).
PURPOSE_ACCOUNT = "email_verification"
#: Proving an address for a Google/GitHub identity whose provider supplied no
#: verified one. There is no account yet; the code decides which it becomes.
PURPOSE_OAUTH = "oauth_signup"

CODE_DIGITS = 6


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class OtpPolicy:
    """The tunables, read from `Settings` so none is hard-coded at a call site."""

    ttl_minutes: int = 10
    max_attempts: int = 5
    resend_cooldown_seconds: int = 60
    max_sends_per_window: int = 5
    window_minutes: int = 60
    verification_ttl_minutes: int = 60

    @classmethod
    def from_settings(cls, settings) -> "OtpPolicy":
        return cls(
            ttl_minutes=settings.otp_ttl_minutes,
            max_attempts=settings.otp_max_attempts,
            resend_cooldown_seconds=settings.otp_resend_cooldown_seconds,
            max_sends_per_window=settings.otp_max_sends_per_window,
            window_minutes=settings.otp_send_window_minutes,
            verification_ttl_minutes=settings.verification_ttl_minutes,
        )


@dataclass(frozen=True)
class Verification:
    verification_id: str
    purpose: str
    user_id: str | None
    email: str | None
    decoy: bool
    provider: str | None
    provider_subject: str | None
    provider_display_name: str | None
    created_at: datetime
    expires_at: datetime
    completed_at: datetime | None

    def is_live(self, now: datetime | None = None) -> bool:
        now = now or _now()
        return self.completed_at is None and now < self.expires_at


def _row_to_verification(row: sqlite3.Row) -> Verification:
    return Verification(
        verification_id=row["verification_id"],
        purpose=row["purpose"],
        user_id=row["user_id"],
        email=row["email"],
        decoy=bool(row["decoy"]),
        provider=row["provider"],
        provider_subject=row["provider_subject"],
        provider_display_name=row["provider_display_name"],
        created_at=_parse(row["created_at_utc"]),  # type: ignore[arg-type]
        expires_at=_parse(row["expires_at_utc"]),  # type: ignore[arg-type]
        completed_at=_parse(row["completed_at_utc"]),
    )


class VerifyResult(StrEnum):
    VERIFIED = "verified"
    INVALID = "invalid"
    EXPIRED = "expired"
    ATTEMPTS_EXCEEDED = "attempts_exceeded"


@dataclass(frozen=True)
class VerifyOutcome:
    result: VerifyResult
    attempts_remaining: int = 0


class SendRefused(Exception):
    """A code was asked for too soon, or too often."""

    def __init__(self, reason: str, retry_after_seconds: int) -> None:
        super().__init__(reason)
        self.reason = reason  # "cooldown" | "limit"
        self.retry_after_seconds = max(1, int(retry_after_seconds))


@dataclass(frozen=True)
class IssuedCode:
    otp_id: str
    code: str
    expires_at: datetime


def mask_email(email: str | None) -> str | None:
    """`sarah@example.com` -> `s••••@example.com`. Enough to recognise, no more."""
    if not email or "@" not in email:
        return None
    local, domain = email.rsplit("@", 1)
    return f"{local[:1]}••••@{domain}"


def _code_hash(salt: str, verification_id: str, code: str) -> str:
    return hmac.new(
        bytes.fromhex(salt),
        f"{verification_id}:{code}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _normalise_code(code: str) -> str | None:
    """Digits only, with spaces and dashes forgiven (people paste `123 456`)."""
    cleaned = "".join(ch for ch in (code or "") if ch not in " -\t")
    if len(cleaned) != CODE_DIGITS or not cleaned.isascii() or not cleaned.isdigit():
        return None
    return cleaned


# --- verifications ----------------------------------------------------------


def open_verification(
    conn: sqlite3.Connection,
    *,
    purpose: str,
    policy: OtpPolicy,
    user_id: str | None = None,
    email: str | None = None,
    decoy: bool = False,
    provider: str | None = None,
    provider_subject: str | None = None,
    provider_display_name: str | None = None,
) -> tuple[str, Verification]:
    """Start a verification. Returns the cookie value (shown once) and the row."""
    token = new_token()
    now = _now()
    verification_id = new_id("VER")
    with conn:
        conn.execute(
            """
            INSERT INTO email_verifications
                (verification_id, token_hash, purpose, user_id, email, decoy,
                 provider, provider_subject, provider_display_name,
                 created_at_utc, expires_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                verification_id,
                hash_token(token),
                purpose,
                user_id,
                repo.normalize_email(email) if email else None,
                1 if decoy else 0,
                provider,
                provider_subject,
                provider_display_name,
                now.isoformat(),
                (now + timedelta(minutes=policy.verification_ttl_minutes)).isoformat(),
            ),
        )
    verification = get_verification(conn, verification_id)
    assert verification is not None
    return token, verification


def get_verification(conn: sqlite3.Connection, verification_id: str) -> Verification | None:
    row = conn.execute(
        "SELECT * FROM email_verifications WHERE verification_id = ?",
        (verification_id,),
    ).fetchone()
    return None if row is None else _row_to_verification(row)


def find_verification(conn: sqlite3.Connection, token: str | None) -> Verification | None:
    """The live verification this cookie value names, or None."""
    if not token:
        return None
    row = conn.execute(
        "SELECT * FROM email_verifications WHERE token_hash = ?", (hash_token(token),)
    ).fetchone()
    if row is None:
        return None
    verification = _row_to_verification(row)
    return verification if verification.is_live() else None


def complete(conn: sqlite3.Connection, verification: Verification) -> bool:
    """Close a verification. True only for the one caller that closed it."""
    with conn:
        cursor = conn.execute(
            """
            UPDATE email_verifications SET completed_at_utc = ?
             WHERE verification_id = ? AND completed_at_utc IS NULL
            """,
            (_now().isoformat(), verification.verification_id),
        )
    return cursor.rowcount == 1


def close_for_user(conn: sqlite3.Connection, user_id: str) -> None:
    """Close every open verification for an account whose address is now settled.

    Called when an address is verified by any route, so a code still sitting in
    another browser's verification cannot be spent afterwards.
    """
    now = _now().isoformat()
    with conn:
        conn.execute(
            """
            UPDATE email_otps SET superseded_at_utc = ?
             WHERE superseded_at_utc IS NULL AND consumed_at_utc IS NULL
               AND verification_id IN (
                   SELECT verification_id FROM email_verifications
                    WHERE user_id = ? AND completed_at_utc IS NULL)
            """,
            (now, user_id),
        )
        conn.execute(
            """
            UPDATE email_verifications SET completed_at_utc = ?
             WHERE user_id = ? AND completed_at_utc IS NULL
            """,
            (now, user_id),
        )


def set_email(
    conn: sqlite3.Connection, verification: Verification, email: str
) -> Verification:
    """Record the address a provider sign-in wants to prove. Retires old codes."""
    if verification.purpose != PURPOSE_OAUTH:
        raise ValueError("Only a provider sign-in chooses its own address.")
    normalized = repo.normalize_email(email)
    now = _now().isoformat()
    with conn:
        conn.execute(
            """
            UPDATE email_otps SET superseded_at_utc = ?
             WHERE verification_id = ? AND superseded_at_utc IS NULL
               AND consumed_at_utc IS NULL
            """,
            (now, verification.verification_id),
        )
        conn.execute(
            "UPDATE email_verifications SET email = ? WHERE verification_id = ?",
            (normalized, verification.verification_id),
        )
    updated = get_verification(conn, verification.verification_id)
    assert updated is not None
    return updated


# --- codes ------------------------------------------------------------------


def _latest_code(conn: sqlite3.Connection, verification_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM email_otps WHERE verification_id = ?
         ORDER BY created_at_utc DESC, rowid DESC LIMIT 1
        """,
        (verification_id,),
    ).fetchone()


def _sends_in_window(
    conn: sqlite3.Connection, *, column: str, value: str, since: datetime
) -> list[datetime]:
    # `column` is one of two literals chosen below, never caller input.
    assert column in {"verification_id", "email"}
    rows = conn.execute(
        f"SELECT created_at_utc FROM email_otps WHERE {column} = ? "
        "AND created_at_utc >= ? ORDER BY created_at_utc",
        (value, since.isoformat()),
    ).fetchall()
    return [_parse(row["created_at_utc"]) for row in rows]  # type: ignore[misc]


def send_allowance(
    conn: sqlite3.Connection, verification: Verification, policy: OtpPolicy
) -> tuple[int, int]:
    """(seconds until another code may be sent, codes left in this window)."""
    now = _now()
    since = now - timedelta(minutes=policy.window_minutes)
    wait = 0

    latest = _latest_code(conn, verification.verification_id)
    if latest is not None:
        sent_at = _parse(latest["created_at_utc"])
        assert sent_at is not None
        elapsed = (now - sent_at).total_seconds()
        if elapsed < policy.resend_cooldown_seconds:
            wait = int(policy.resend_cooldown_seconds - elapsed) + 1

    windows = [
        _sends_in_window(
            conn, column="verification_id", value=verification.verification_id, since=since
        )
    ]
    # A decoy never counts against, or is limited by, the real owner's address.
    if verification.email and not verification.decoy:
        windows.append(
            _sends_in_window(conn, column="email", value=verification.email, since=since)
        )

    remaining = policy.max_sends_per_window
    for sends in windows:
        remaining = min(remaining, policy.max_sends_per_window - len(sends))
        if len(sends) >= policy.max_sends_per_window:
            oldest = sends[len(sends) - policy.max_sends_per_window]
            reopen = oldest + timedelta(minutes=policy.window_minutes)
            wait = max(wait, int((reopen - now).total_seconds()) + 1)
    return wait, max(0, remaining)


def issue_code(
    conn: sqlite3.Connection, verification: Verification, policy: OtpPolicy
) -> IssuedCode:
    """Mint a fresh code, retiring every earlier one. Raises `SendRefused`.

    The plaintext code is returned so the caller can email it, and exists
    nowhere else: not in the database, not in a log, not in a response.
    """
    if verification.email is None:
        raise ValueError("A code needs an address to go to.")

    wait, remaining = send_allowance(conn, verification, policy)
    if remaining <= 0:
        raise SendRefused("limit", wait)
    if wait > 0:
        raise SendRefused("cooldown", wait)

    code = f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"
    salt = secrets.token_hex(16)
    now = _now()
    expires_at = now + timedelta(minutes=policy.ttl_minutes)
    otp_id = new_id("OTP")
    with conn:
        # Only the newest code works: one for this verification, and — for a
        # real address — any other browser's outstanding code for it too.
        conn.execute(
            """
            UPDATE email_otps SET superseded_at_utc = ?
             WHERE superseded_at_utc IS NULL AND consumed_at_utc IS NULL
               AND (verification_id = ? OR (? = 0 AND email = ?))
            """,
            (
                now.isoformat(),
                verification.verification_id,
                1 if verification.decoy else 0,
                verification.email,
            ),
        )
        conn.execute(
            """
            INSERT INTO email_otps
                (otp_id, verification_id, email, code_salt, code_hash,
                 created_at_utc, expires_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                otp_id,
                verification.verification_id,
                None if verification.decoy else verification.email,
                salt,
                _code_hash(salt, verification.verification_id, code),
                now.isoformat(),
                expires_at.isoformat(),
            ),
        )
    return IssuedCode(otp_id=otp_id, code=code, expires_at=expires_at)


def mark_delivered(conn: sqlite3.Connection, otp_id: str) -> None:
    with conn:
        conn.execute("UPDATE email_otps SET delivered = 1 WHERE otp_id = ?", (otp_id,))


def verify_code(
    conn: sqlite3.Connection,
    verification: Verification,
    code: str,
    policy: OtpPolicy,
) -> VerifyOutcome:
    """Check a submitted code against the newest live one.

    Every submission spends an attempt — including one that is not even six
    digits — and the attempt is spent *before* the comparison, under a guarded
    `UPDATE`, so concurrent guesses cannot exceed the limit between them.
    """
    row = _latest_code(conn, verification.verification_id)
    now = _now()
    if row is None or row["superseded_at_utc"] is not None or row["consumed_at_utc"]:
        return VerifyOutcome(VerifyResult.EXPIRED)
    expires_at = _parse(row["expires_at_utc"])
    if expires_at is None or now >= expires_at:
        return VerifyOutcome(VerifyResult.EXPIRED)
    if row["attempts"] >= policy.max_attempts:
        return VerifyOutcome(VerifyResult.ATTEMPTS_EXCEEDED)

    with conn:
        spent = conn.execute(
            """
            UPDATE email_otps SET attempts = attempts + 1
             WHERE otp_id = ? AND attempts < ? AND consumed_at_utc IS NULL
               AND superseded_at_utc IS NULL
            """,
            (row["otp_id"], policy.max_attempts),
        )
    if spent.rowcount != 1:
        return VerifyOutcome(VerifyResult.ATTEMPTS_EXCEEDED)
    remaining = policy.max_attempts - (row["attempts"] + 1)

    submitted = _normalise_code(code)
    matches = submitted is not None and hmac.compare_digest(
        _code_hash(row["code_salt"], verification.verification_id, submitted),
        row["code_hash"],
    )
    if not matches or verification.decoy:
        if remaining <= 0:
            return VerifyOutcome(VerifyResult.ATTEMPTS_EXCEEDED)
        return VerifyOutcome(VerifyResult.INVALID, attempts_remaining=remaining)

    with conn:
        consumed = conn.execute(
            """
            UPDATE email_otps SET consumed_at_utc = ?
             WHERE otp_id = ? AND consumed_at_utc IS NULL AND superseded_at_utc IS NULL
            """,
            (now.isoformat(), row["otp_id"]),
        )
    if consumed.rowcount != 1:
        return VerifyOutcome(VerifyResult.EXPIRED)
    return VerifyOutcome(VerifyResult.VERIFIED)


def status(
    conn: sqlite3.Connection,
    verification: Verification,
    policy: OtpPolicy,
    *,
    email_sent: bool | None = None,
) -> dict:
    """What the verification screen needs to render. Never the code."""
    now = _now()
    wait, remaining = send_allowance(conn, verification, policy)
    latest = _latest_code(conn, verification.verification_id)
    code_expires_in: int | None = None
    attempts_remaining: int | None = None
    if (
        latest is not None
        and latest["superseded_at_utc"] is None
        and latest["consumed_at_utc"] is None
    ):
        expires_at = _parse(latest["expires_at_utc"])
        assert expires_at is not None
        code_expires_in = max(0, int((expires_at - now).total_seconds()))
        attempts_remaining = max(0, policy.max_attempts - latest["attempts"])

    body: dict = {
        "purpose": verification.purpose,
        "email_hint": mask_email(verification.email),
        "needs_email": verification.email is None,
        "provider": verification.provider,
        "code_expires_in_seconds": code_expires_in,
        "code_ttl_seconds": policy.ttl_minutes * 60,
        "attempts_remaining": attempts_remaining,
        "resend_in_seconds": wait,
        "sends_remaining": remaining,
        "expires_in_seconds": max(0, int((verification.expires_at - now).total_seconds())),
    }
    if email_sent is not None:
        body["email_sent"] = email_sent
    return body
