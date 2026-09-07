"""The authentication flows, and the decisions that make them safe.

Registration, login, second factor, verification, reset, logout. The
repository below stores things; this module decides *what may happen*, and
three of those decisions are worth stating up front because they are the ones
usually got wrong.

**Nothing here tells a stranger whether an account exists.** Registration with
a taken address, login with an unknown address, and a reset request for an
unknown address all return exactly what their successful counterparts return.
The login path additionally spends the same scrypt work on a missing user that
it would have spent verifying a real hash (`passwords.waste_time`), because an
enumeration oracle built from response *timing* is just as good as one built
from response text.

**Failure counting is per-account and per-client, and both must pass.** An
attacker spraying one password across many accounts never trips a per-account
counter, and an attacker hammering one account from a botnet never trips a
per-client one. Counting only one of the two leaves the other wide open.

**A password is one factor.** When MFA is enabled, a correct password issues a
session marked `mfa_satisfied = 0`, which the API dependency treats as
authenticated for exactly one endpoint — the challenge — and for nothing else.
The half-session is a real session so that the challenge can be rate-limited
and audited per user, rather than a bag of state keyed by an email address.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.backend.auth import repository as repo
from app.backend.auth import totp
from app.backend.auth.passwords import (
    PasswordError,
    hash_password,
    needs_rehash,
    validate_password,
    verify_password,
    waste_time,
)
from app.backend.services.audit import (
    AuditEvent,
    AuditOutcome,
    hash_identifier,
    record_event,
)

# --- brute-force thresholds -------------------------------------------------
#
# Counted over a rolling window rather than reset on success, so an attacker
# cannot clear the counter by interleaving a login they do control.
MAX_FAILURES_PER_ACCOUNT = 5
MAX_FAILURES_PER_CLIENT = 20
LOCKOUT_WINDOW_MINUTES = 15

VERIFICATION_PURPOSE = "email_verification"
RESET_PURPOSE = "password_reset"

#: Returned by `request_password_reset` whatever the address was, so the
#: response cannot be used to test whether an account exists.
RESET_ACKNOWLEDGEMENT = (
    "If an account exists for that address, a password reset link has been sent."
)


class AuthError(Exception):
    """Authentication or registration was refused."""

    code = "authentication_failed"


class InvalidCredentials(AuthError):
    """Wrong password, unknown account, or a disabled one — deliberately one type.

    A single exception for all three is what stops a caller distinguishing
    them: the route has nothing to branch on even if it wanted to.
    """

    code = "invalid_credentials"


class AccountLocked(AuthError):
    code = "account_locked"


class MfaRequired(AuthError):
    """The password was right and a second factor is outstanding."""

    code = "mfa_required"


class RegistrationError(AuthError):
    code = "registration_failed"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LoginResult:
    """What a successful password step produced.

    `mfa_pending` distinguishes a finished login from a half-finished one. The
    session token is issued either way; what differs is what the session may
    reach, which the API dependency enforces.
    """

    session_id: str
    session_token: str
    user_id: str
    mfa_pending: bool
    org_id: str | None


# --- failure counting -------------------------------------------------------


def _record_attempt(
    conn: sqlite3.Connection, *, identifier: str, scope: str, successful: bool
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO login_attempts
                (identifier_hash, scope, attempted_at_utc, successful)
            VALUES (?, ?, ?, ?)
            """,
            (
                hashlib.sha256(identifier.encode("utf-8")).hexdigest(),
                scope,
                _now().isoformat(),
                1 if successful else 0,
            ),
        )


def _recent_failures(conn: sqlite3.Connection, *, identifier: str, scope: str) -> int:
    since = (_now() - timedelta(minutes=LOCKOUT_WINDOW_MINUTES)).isoformat()
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM login_attempts
         WHERE identifier_hash = ? AND scope = ? AND successful = 0
           AND attempted_at_utc >= ?
        """,
        (hashlib.sha256(identifier.encode("utf-8")).hexdigest(), scope, since),
    ).fetchone()
    return int(row["n"]) if row else 0


def is_locked_out(
    conn: sqlite3.Connection, *, email: str, client_ip: str | None
) -> bool:
    """True when either counter is over its threshold.

    Checked *before* the password is verified, so a locked account costs an
    attacker a database read rather than a scrypt derivation — otherwise the
    lockout itself becomes the denial-of-service.
    """
    normalized = repo.normalize_email(email)
    if _recent_failures(conn, identifier=normalized, scope="account") >= (
        MAX_FAILURES_PER_ACCOUNT
    ):
        return True
    if client_ip and _recent_failures(conn, identifier=client_ip, scope="client") >= (
        MAX_FAILURES_PER_CLIENT
    ):
        return True
    return False


def _note_failure(
    conn: sqlite3.Connection, *, email: str, client_ip: str | None
) -> None:
    _record_attempt(
        conn, identifier=repo.normalize_email(email), scope="account", successful=False
    )
    if client_ip:
        _record_attempt(conn, identifier=client_ip, scope="client", successful=False)


# --- registration -----------------------------------------------------------


def register_user(
    conn: sqlite3.Connection,
    *,
    email: str,
    password: str,
    display_name: str,
    request_id: str | None = None,
    client_ip: str | None = None,
) -> tuple[str, str | None]:
    """Create an account. Returns (user_id, verification token or None).

    Raises `RegistrationError` only for a password that cannot be stored. An
    address that is already registered does **not** raise: it returns a
    synthetic result so the caller's response is identical either way, and the
    real owner is the only party who learns anything (they would receive the
    verification mail).
    """
    normalized = repo.normalize_email(email)
    if "@" not in normalized or len(normalized) > 320:
        raise RegistrationError("A valid email address is required.")
    if not display_name or not display_name.strip():
        raise RegistrationError("A display name is required.")

    try:
        validate_password(password)
    except PasswordError as exc:
        raise RegistrationError(str(exc)) from exc

    existing = repo.get_user_by_email(conn, normalized)
    if existing is not None:
        # Do the same work, record the collision for defenders, and hand back
        # a shape the caller cannot tell from success.
        waste_time()
        record_event(
            conn,
            AuditEvent.REGISTERED,
            outcome=AuditOutcome.FAILURE,
            request_id=request_id,
            ip_hash=hash_identifier(client_ip),
            details={"reason": "email_already_registered"},
        )
        return existing.user_id, None

    user = repo.create_user(
        conn,
        email=normalized,
        display_name=display_name.strip()[:200],
        password_hash=hash_password(password),
    )
    token = repo.issue_auth_token(
        conn,
        user_id=user.user_id,
        purpose=VERIFICATION_PURPOSE,
        ttl_minutes=repo.EMAIL_VERIFICATION_TTL_HOURS * 60,
    )
    record_event(
        conn,
        AuditEvent.REGISTERED,
        actor_user_id=user.user_id,
        request_id=request_id,
        ip_hash=hash_identifier(client_ip),
        details={"email_domain": normalized.split("@")[-1]},
    )
    return user.user_id, token


def verify_email(
    conn: sqlite3.Connection, *, token: str, request_id: str | None = None
) -> bool:
    user_id = repo.consume_auth_token(conn, token=token, purpose=VERIFICATION_PURPOSE)
    if user_id is None:
        return False
    repo.mark_email_verified(conn, user_id)
    record_event(
        conn,
        AuditEvent.EMAIL_VERIFIED,
        actor_user_id=user_id,
        request_id=request_id,
    )
    return True


# --- login ------------------------------------------------------------------


def login(
    conn: sqlite3.Connection,
    *,
    email: str,
    password: str,
    client_ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
    require_verified_email: bool = True,
) -> LoginResult:
    """Verify a password and issue a session.

    Raises `InvalidCredentials` for every failure mode a stranger could probe —
    unknown address, wrong password, disabled account, unverified address — so
    none of them is distinguishable from the others.
    """
    normalized = repo.normalize_email(email)
    ip_hash = hash_identifier(client_ip)

    if is_locked_out(conn, email=normalized, client_ip=client_ip):
        record_event(
            conn,
            AuditEvent.LOGIN_LOCKED_OUT,
            outcome=AuditOutcome.DENIED,
            request_id=request_id,
            ip_hash=ip_hash,
            details={"window_minutes": LOCKOUT_WINDOW_MINUTES},
        )
        raise AccountLocked(
            "Too many failed attempts. Try again in "
            f"{LOCKOUT_WINDOW_MINUTES} minutes."
        )

    user = repo.get_user_by_email(conn, normalized)
    if user is None:
        # Same cost as a real verification, so timing reveals nothing.
        waste_time()
        _note_failure(conn, email=normalized, client_ip=client_ip)
        record_event(
            conn,
            AuditEvent.LOGIN_FAILED,
            outcome=AuditOutcome.FAILURE,
            request_id=request_id,
            ip_hash=ip_hash,
            details={"reason": "unknown_account"},
        )
        raise InvalidCredentials("Incorrect email address or password.")

    if not verify_password(password, user.password_hash):
        _note_failure(conn, email=normalized, client_ip=client_ip)
        record_event(
            conn,
            AuditEvent.LOGIN_FAILED,
            outcome=AuditOutcome.FAILURE,
            actor_user_id=user.user_id,
            request_id=request_id,
            ip_hash=ip_hash,
            details={"reason": "bad_password"},
        )
        raise InvalidCredentials("Incorrect email address or password.")

    # The password was right. Everything below is a property of the account,
    # and each still refuses with the same message a wrong password gets.
    if not user.is_active:
        record_event(
            conn,
            AuditEvent.LOGIN_FAILED,
            outcome=AuditOutcome.DENIED,
            actor_user_id=user.user_id,
            request_id=request_id,
            ip_hash=ip_hash,
            details={"reason": "account_disabled"},
        )
        raise InvalidCredentials("Incorrect email address or password.")

    if require_verified_email and not user.email_verified:
        record_event(
            conn,
            AuditEvent.LOGIN_FAILED,
            outcome=AuditOutcome.DENIED,
            actor_user_id=user.user_id,
            request_id=request_id,
            ip_hash=ip_hash,
            details={"reason": "email_unverified"},
        )
        raise InvalidCredentials("Incorrect email address or password.")

    # Opportunistically upgrade a hash derived under older parameters.
    if needs_rehash(user.password_hash):
        repo.set_password_hash(conn, user.user_id, hash_password(password))

    _record_attempt(conn, identifier=normalized, scope="account", successful=True)

    memberships = repo.list_memberships(conn, user.user_id)
    org_id = memberships[0].org_id if memberships else None

    # A brand-new session id on every login: an attacker who fixed a session
    # value before authentication holds a value that was never elevated.
    session_id, token = repo.create_session(
        conn,
        user_id=user.user_id,
        org_id=org_id,
        mfa_satisfied=not user.mfa_enabled,
        ip_hash=ip_hash,
        user_agent_hash=hash_identifier(user_agent),
    )

    if user.mfa_enabled:
        record_event(
            conn,
            AuditEvent.LOGIN_SUCCEEDED,
            outcome=AuditOutcome.SUCCESS,
            actor_user_id=user.user_id,
            org_id=org_id,
            request_id=request_id,
            ip_hash=ip_hash,
            details={"stage": "password_only", "mfa_pending": True},
        )
        return LoginResult(session_id, token, user.user_id, True, org_id)

    repo.record_login(conn, user.user_id)
    record_event(
        conn,
        AuditEvent.LOGIN_SUCCEEDED,
        actor_user_id=user.user_id,
        org_id=org_id,
        request_id=request_id,
        ip_hash=ip_hash,
        details={"stage": "complete", "mfa_pending": False},
    )
    return LoginResult(session_id, token, user.user_id, False, org_id)


def complete_mfa(
    conn: sqlite3.Connection,
    *,
    session: repo.Session,
    code: str,
    request_id: str | None = None,
    client_ip: str | None = None,
) -> bool:
    """Satisfy the second factor on a half-authenticated session.

    Marks the session rather than issuing a new one: the session already
    exists, is already bound to the user, and only its `mfa_satisfied` flag is
    in question. The spent time-step is recorded so the same code cannot be
    presented again inside its acceptance window.
    """
    user = repo.get_user(conn, session.user_id)
    if user is None or not user.mfa_enabled or not user.mfa_secret:
        return False

    step = totp.verify(user.mfa_secret, code, last_used_step=user.mfa_last_step)
    if step is None:
        _note_failure(conn, email=user.email, client_ip=client_ip)
        record_event(
            conn,
            AuditEvent.MFA_CHALLENGE_FAILED,
            outcome=AuditOutcome.FAILURE,
            actor_user_id=user.user_id,
            request_id=request_id,
            ip_hash=hash_identifier(client_ip),
        )
        return False

    repo.record_mfa_step(conn, user.user_id, step)
    repo.mark_session_mfa_satisfied(conn, session.session_id)
    repo.record_login(conn, user.user_id)
    record_event(
        conn,
        AuditEvent.LOGIN_SUCCEEDED,
        actor_user_id=user.user_id,
        org_id=session.org_id,
        request_id=request_id,
        ip_hash=hash_identifier(client_ip),
        details={"stage": "mfa_complete"},
    )
    return True


def logout(
    conn: sqlite3.Connection, *, session: repo.Session, request_id: str | None = None
) -> None:
    repo.revoke_session(conn, session.session_id)
    record_event(
        conn,
        AuditEvent.LOGOUT,
        actor_user_id=session.user_id,
        org_id=session.org_id,
        request_id=request_id,
    )


# --- MFA enrolment ----------------------------------------------------------


def begin_mfa_enrolment(
    conn: sqlite3.Connection, *, user_id: str, issuer: str = "ASTRION",
    request_id: str | None = None,
) -> tuple[str, str]:
    """Generate a secret and its provisioning URI. Does not enable MFA.

    MFA turns on only once the user proves they can produce a code
    (`confirm_mfa_enrolment`). Enabling it here would lock out anyone whose
    authenticator failed to save the secret.
    """
    user = repo.get_user(conn, user_id)
    if user is None:
        raise AuthError("No such user.")
    secret = totp.new_secret()
    repo.set_mfa_secret(conn, user_id, secret)
    record_event(
        conn,
        AuditEvent.MFA_ENROLMENT_STARTED,
        actor_user_id=user_id,
        request_id=request_id,
    )
    return secret, totp.provisioning_uri(
        secret, account_name=user.email, issuer=issuer
    )


def confirm_mfa_enrolment(
    conn: sqlite3.Connection, *, user_id: str, code: str,
    request_id: str | None = None,
) -> bool:
    user = repo.get_user(conn, user_id)
    if user is None or not user.mfa_secret:
        return False
    step = totp.verify(user.mfa_secret, code, last_used_step=user.mfa_last_step)
    if step is None:
        record_event(
            conn,
            AuditEvent.MFA_CHALLENGE_FAILED,
            outcome=AuditOutcome.FAILURE,
            actor_user_id=user_id,
            request_id=request_id,
            details={"stage": "enrolment"},
        )
        return False
    repo.record_mfa_step(conn, user_id, step)
    repo.set_mfa_enabled(conn, user_id, True)
    record_event(conn, AuditEvent.MFA_ENABLED, actor_user_id=user_id, request_id=request_id)
    return True


def disable_mfa(
    conn: sqlite3.Connection, *, user_id: str, password: str,
    request_id: str | None = None,
) -> bool:
    """Turn MFA off. Requires the password again.

    Re-authenticating for this matters: without it, anyone who walks up to an
    unlocked, already-authenticated browser can strip the second factor off the
    account permanently.
    """
    user = repo.get_user(conn, user_id)
    if user is None or not verify_password(password, user.password_hash):
        record_event(
            conn,
            AuditEvent.MFA_DISABLED,
            outcome=AuditOutcome.DENIED,
            actor_user_id=user_id,
            request_id=request_id,
            details={"reason": "reauthentication_failed"},
        )
        return False
    repo.set_mfa_enabled(conn, user_id, False)
    record_event(conn, AuditEvent.MFA_DISABLED, actor_user_id=user_id, request_id=request_id)
    return True


# --- password reset and change ----------------------------------------------


def request_password_reset(
    conn: sqlite3.Connection,
    *,
    email: str,
    request_id: str | None = None,
    client_ip: str | None = None,
) -> str | None:
    """Mint a reset link, or pretend to. Returns the token or None.

    The caller must respond identically in both cases — see
    `RESET_ACKNOWLEDGEMENT`. Returning None rather than raising is what makes
    that easy to do correctly: there is no error path for a route to leak.
    """
    user = repo.get_user_by_email(conn, email)
    record_event(
        conn,
        AuditEvent.PASSWORD_RESET_REQUESTED,
        outcome=AuditOutcome.SUCCESS if user else AuditOutcome.FAILURE,
        actor_user_id=user.user_id if user else None,
        request_id=request_id,
        ip_hash=hash_identifier(client_ip),
        details={"account_exists": bool(user)},
    )
    if user is None or not user.is_active:
        return None
    # Outstanding links are burned first, so requesting a new one invalidates
    # an older one that may be sitting in a forwarded mail.
    repo.invalidate_tokens(conn, user_id=user.user_id, purpose=RESET_PURPOSE)
    return repo.issue_auth_token(
        conn,
        user_id=user.user_id,
        purpose=RESET_PURPOSE,
        ttl_minutes=repo.PASSWORD_RESET_TTL_MINUTES,
    )


def complete_password_reset(
    conn: sqlite3.Connection,
    *,
    token: str,
    new_password: str,
    request_id: str | None = None,
) -> bool:
    """Redeem a reset link and set a new password.

    Every session the user holds is revoked. A reset is what someone does when
    they believe an attacker has access; leaving that attacker's session alive
    would defeat the entire exercise.
    """
    try:
        validate_password(new_password)
    except PasswordError as exc:
        raise RegistrationError(str(exc)) from exc

    user_id = repo.consume_auth_token(conn, token=token, purpose=RESET_PURPOSE)
    if user_id is None:
        record_event(
            conn,
            AuditEvent.PASSWORD_RESET_COMPLETED,
            outcome=AuditOutcome.FAILURE,
            request_id=request_id,
            details={"reason": "invalid_or_used_token"},
        )
        return False

    repo.set_password_hash(conn, user_id, hash_password(new_password))
    repo.invalidate_tokens(conn, user_id=user_id, purpose=RESET_PURPOSE)
    revoked = repo.revoke_all_sessions(conn, user_id)
    # A reset also proves control of the mailbox, so it settles verification.
    repo.mark_email_verified(conn, user_id)
    record_event(
        conn,
        AuditEvent.PASSWORD_RESET_COMPLETED,
        actor_user_id=user_id,
        request_id=request_id,
        details={"sessions_revoked": revoked},
    )
    return True


def change_password(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    current_password: str,
    new_password: str,
    keep_session_id: str | None = None,
    request_id: str | None = None,
) -> bool:
    """Change a password from inside an authenticated session.

    Other sessions are revoked; the caller's own is kept so they are not
    signed out of the tab they are using.
    """
    user = repo.get_user(conn, user_id)
    if user is None or not verify_password(current_password, user.password_hash):
        record_event(
            conn,
            AuditEvent.PASSWORD_CHANGED,
            outcome=AuditOutcome.DENIED,
            actor_user_id=user_id,
            request_id=request_id,
            details={"reason": "reauthentication_failed"},
        )
        return False
    try:
        validate_password(new_password)
    except PasswordError as exc:
        raise RegistrationError(str(exc)) from exc

    repo.set_password_hash(conn, user_id, hash_password(new_password))
    revoked = repo.revoke_all_sessions(conn, user_id, except_session_id=keep_session_id)
    record_event(
        conn,
        AuditEvent.PASSWORD_CHANGED,
        actor_user_id=user_id,
        request_id=request_id,
        details={"sessions_revoked": revoked},
    )
    return True


# --- provisioning helper ----------------------------------------------------


def provision_organization(
    conn: sqlite3.Connection,
    *,
    owner_user_id: str,
    name: str,
    slug: str | None = None,
    account_ids: list[str] | None = None,
    request_id: str | None = None,
) -> str:
    """Create a workspace with its first owner and its tenant scope.

    A thin wrapper over `auth/workspaces.py::create_workspace` so that the
    bootstrap script, the tests and the API all create workspaces through one
    code path — including its owner assignment, its ceiling and its audit
    events. `slug` is accepted for call-site compatibility and ignored: the
    slug is derived from the name and de-duplicated, so it is not something a
    caller needs to pick correctly.
    """
    from app.backend.auth.workspaces import create_workspace

    workspace = create_workspace(
        conn,
        owner_user_id=owner_user_id,
        name=name,
        account_ids=account_ids,
        request_id=request_id,
    )
    return workspace["org_id"]
