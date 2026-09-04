"""Data access for users, organisations, memberships and sessions.

Parameterized SQL only, typed results out, exactly like
`services/records.py` — and for the same reason: no function here accepts a
SQL string or fragment from a caller, so nothing above it can widen a query.

The one function that carries the most weight is `accounts_for_membership`.
It is where tenancy stops being a data model and becomes an enforcement
boundary: it turns "this session belongs to this organisation" into the
`allowed_account_ids` set that every scoped query below the tool layer already
filters on. Because that set is *derived here from the session's organisation*
and never read from the request, a client cannot widen it by any means the
HTTP layer offers.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.backend.auth.permissions import OrgRole, Permission, role_has
from app.backend.auth.tokens import hash_token, new_id, new_token

# --- session lifetimes ------------------------------------------------------
#
# Two clocks, because they defend different things. The idle timeout limits how
# long an abandoned browser stays usable; the absolute timeout bounds the value
# of a stolen cookie no matter how actively it is used.
IDLE_TIMEOUT_MINUTES = 60
ABSOLUTE_TIMEOUT_HOURS = 12

#: Verification and reset links. Short, because a link that lingers in an inbox
#: is a credential that lingers in an inbox.
EMAIL_VERIFICATION_TTL_HOURS = 24
PASSWORD_RESET_TTL_MINUTES = 30

ACTIVE = "active"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def normalize_email(email: str) -> str:
    """Lower-case and strip. The stored form, and the only form ever compared.

    Normalising at every boundary rather than only at registration is what
    stops `User@x.com` and `user@x.com` becoming two accounts, and stops a
    lookup missing a user who typed their address with different capitals.
    """
    return (email or "").strip().lower()


@dataclass(frozen=True)
class User:
    user_id: str
    email: str
    display_name: str
    email_verified: bool
    mfa_enabled: bool
    status: str
    password_hash: str
    mfa_secret: str | None = None
    mfa_last_step: int | None = None

    @property
    def is_active(self) -> bool:
        return self.status == ACTIVE


@dataclass(frozen=True)
class Membership:
    """A user's role in one organisation, with the tenant scope it confers."""

    membership_id: str
    org_id: str
    org_name: str
    user_id: str
    role: OrgRole
    status: str

    @property
    def is_active(self) -> bool:
        return self.status == ACTIVE

    def has(self, permission: Permission) -> bool:
        return self.is_active and role_has(self.role, permission)


@dataclass(frozen=True)
class Session:
    session_id: str
    user_id: str
    org_id: str | None
    mfa_satisfied: bool
    created_at: datetime
    absolute_expires_at: datetime
    idle_expires_at: datetime
    revoked_at: datetime | None

    def is_live(self, now: datetime | None = None) -> bool:
        now = now or _now()
        return (
            self.revoked_at is None
            and now < self.absolute_expires_at
            and now < self.idle_expires_at
        )


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        user_id=row["user_id"],
        email=row["email"],
        display_name=row["display_name"],
        email_verified=bool(row["email_verified"]),
        mfa_enabled=bool(row["mfa_enabled"]),
        status=row["status"],
        password_hash=row["password_hash"],
        mfa_secret=row["mfa_secret"],
        mfa_last_step=row["mfa_last_step"],
    )


# --- users ------------------------------------------------------------------


def create_user(
    conn: sqlite3.Connection,
    *,
    email: str,
    display_name: str,
    password_hash: str,
    email_verified: bool = False,
) -> User:
    """Insert a user. Raises `sqlite3.IntegrityError` if the email is taken.

    The collision is deliberately left to the UNIQUE constraint rather than
    checked first: a read-then-write would race two concurrent registrations
    into two accounts for one address.
    """
    user_id = new_id("USR")
    now = _now().isoformat()
    with conn:
        conn.execute(
            """
            INSERT INTO users
                (user_id, email, email_verified, display_name, password_hash,
                 password_updated_at_utc, mfa_enabled, status, created_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                user_id,
                normalize_email(email),
                1 if email_verified else 0,
                display_name.strip(),
                password_hash,
                now,
                ACTIVE,
                now,
            ),
        )
    user = get_user(conn, user_id)
    assert user is not None
    return user


def get_user(conn: sqlite3.Connection, user_id: str) -> User | None:
    row = conn.execute(
        "SELECT * FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    return None if row is None else _row_to_user(row)


def get_user_by_email(conn: sqlite3.Connection, email: str) -> User | None:
    row = conn.execute(
        "SELECT * FROM users WHERE email = ?", (normalize_email(email),)
    ).fetchone()
    return None if row is None else _row_to_user(row)


def set_password_hash(conn: sqlite3.Connection, user_id: str, password_hash: str) -> None:
    with conn:
        conn.execute(
            """
            UPDATE users SET password_hash = ?, password_updated_at_utc = ?
             WHERE user_id = ?
            """,
            (password_hash, _now().isoformat(), user_id),
        )


def mark_email_verified(conn: sqlite3.Connection, user_id: str) -> None:
    with conn:
        conn.execute(
            "UPDATE users SET email_verified = 1 WHERE user_id = ?", (user_id,)
        )


def record_login(conn: sqlite3.Connection, user_id: str) -> None:
    with conn:
        conn.execute(
            "UPDATE users SET last_login_at_utc = ? WHERE user_id = ?",
            (_now().isoformat(), user_id),
        )


def set_mfa_secret(conn: sqlite3.Connection, user_id: str, secret: str | None) -> None:
    """Store (or clear) a TOTP secret. Enrolment only — does not enable MFA.

    Separating "has a secret" from "MFA is on" is what stops a user who
    abandons enrolment halfway from being locked out of their own account at
    the next login.
    """
    with conn:
        conn.execute(
            "UPDATE users SET mfa_secret = ? WHERE user_id = ?", (secret, user_id)
        )


def set_mfa_enabled(conn: sqlite3.Connection, user_id: str, enabled: bool) -> None:
    with conn:
        conn.execute(
            "UPDATE users SET mfa_enabled = ? WHERE user_id = ?",
            (1 if enabled else 0, user_id),
        )
        if not enabled:
            conn.execute(
                "UPDATE users SET mfa_secret = NULL, mfa_last_step = NULL "
                "WHERE user_id = ?",
                (user_id,),
            )


def record_mfa_step(conn: sqlite3.Connection, user_id: str, step: int) -> None:
    """Spend a TOTP time-step so the same code cannot be presented twice."""
    with conn:
        conn.execute(
            "UPDATE users SET mfa_last_step = ? WHERE user_id = ?", (step, user_id)
        )


# --- organisations and membership -------------------------------------------


def create_organization(
    conn: sqlite3.Connection, *, name: str, slug: str
) -> tuple[str, str]:
    org_id = new_id("ORG")
    with conn:
        conn.execute(
            """
            INSERT INTO organizations (org_id, name, slug, status, created_at_utc)
            VALUES (?, ?, ?, ?, ?)
            """,
            (org_id, name.strip(), slug.strip().lower(), ACTIVE, _now().isoformat()),
        )
    return org_id, slug.strip().lower()


def add_member(
    conn: sqlite3.Connection, *, org_id: str, user_id: str, role: OrgRole
) -> str:
    membership_id = new_id("MEM")
    now = _now().isoformat()
    with conn:
        conn.execute(
            """
            INSERT INTO memberships
                (membership_id, org_id, user_id, role, status, created_at_utc,
                 updated_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (membership_id, org_id, user_id, role.value, ACTIVE, now, now),
        )
    return membership_id


def set_member_role(
    conn: sqlite3.Connection, *, org_id: str, user_id: str, role: OrgRole
) -> bool:
    with conn:
        cursor = conn.execute(
            """
            UPDATE memberships SET role = ?, updated_at_utc = ?
             WHERE org_id = ? AND user_id = ?
            """,
            (role.value, _now().isoformat(), org_id, user_id),
        )
    return cursor.rowcount == 1


def remove_member(conn: sqlite3.Connection, *, org_id: str, user_id: str) -> bool:
    """Deactivate rather than delete, so the audit trail keeps its referent."""
    with conn:
        cursor = conn.execute(
            """
            UPDATE memberships SET status = 'removed', updated_at_utc = ?
             WHERE org_id = ? AND user_id = ? AND status = ?
            """,
            (_now().isoformat(), org_id, user_id, ACTIVE),
        )
    return cursor.rowcount == 1


def get_membership(
    conn: sqlite3.Connection, *, org_id: str, user_id: str
) -> Membership | None:
    """The single authorization lookup. Returns None for a non-member.

    None means "not a member of this organisation", which the API surfaces the
    same way it surfaces a missing resource — an attacker probing organisation
    ids must not be able to tell an organisation they cannot see from one that
    does not exist.
    """
    row = conn.execute(
        """
        SELECT m.*, o.name AS org_name, o.status AS org_status
          FROM memberships m
          JOIN organizations o ON o.org_id = m.org_id
         WHERE m.org_id = ? AND m.user_id = ?
        """,
        (org_id, user_id),
    ).fetchone()
    if row is None or row["status"] != ACTIVE or row["org_status"] != ACTIVE:
        return None
    try:
        role = OrgRole(row["role"])
    except ValueError:
        # An unrecognised role denies rather than crashing: a row written by a
        # future version must not fail open here.
        return None
    return Membership(
        membership_id=row["membership_id"],
        org_id=row["org_id"],
        org_name=row["org_name"],
        user_id=row["user_id"],
        role=role,
        status=row["status"],
    )


def list_memberships(conn: sqlite3.Connection, user_id: str) -> list[Membership]:
    rows = conn.execute(
        """
        SELECT m.*, o.name AS org_name, o.status AS org_status
          FROM memberships m
          JOIN organizations o ON o.org_id = m.org_id
         WHERE m.user_id = ? AND m.status = ? AND o.status = ?
         ORDER BY o.name
        """,
        (user_id, ACTIVE, ACTIVE),
    ).fetchall()
    memberships = []
    for row in rows:
        try:
            role = OrgRole(row["role"])
        except ValueError:
            continue
        memberships.append(
            Membership(
                membership_id=row["membership_id"],
                org_id=row["org_id"],
                org_name=row["org_name"],
                user_id=row["user_id"],
                role=role,
                status=row["status"],
            )
        )
    return memberships


def list_org_members(conn: sqlite3.Connection, org_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT m.user_id, m.role, m.status, u.email, u.display_name
          FROM memberships m
          JOIN users u ON u.user_id = m.user_id
         WHERE m.org_id = ? AND m.status = ?
         ORDER BY u.email
        """,
        (org_id, ACTIVE),
    ).fetchall()
    return [dict(row) for row in rows]


# --- tenant scope -----------------------------------------------------------


def grant_account(conn: sqlite3.Connection, *, org_id: str, account_id: str) -> None:
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO organization_accounts
                (org_id, account_id, created_at_utc)
            VALUES (?, ?, ?)
            """,
            (org_id, account_id, _now().isoformat()),
        )


def accounts_for_org(conn: sqlite3.Connection, org_id: str) -> frozenset[str]:
    """Every dataset account this organisation owns.

    This is the tenant boundary, expressed as data. The result becomes
    `AgentContext.allowed_account_ids`, which every repository below the tool
    layer already filters on — so tenancy is enforced by the same code path
    that was already tested, rather than by a new one bolted alongside it.
    """
    rows = conn.execute(
        "SELECT account_id FROM organization_accounts WHERE org_id = ?", (org_id,)
    ).fetchall()
    return frozenset(row["account_id"] for row in rows)


def org_owning_account(conn: sqlite3.Connection, account_id: str) -> str | None:
    row = conn.execute(
        "SELECT org_id FROM organization_accounts WHERE account_id = ?", (account_id,)
    ).fetchone()
    return None if row is None else row["org_id"]


# --- sessions ---------------------------------------------------------------


def create_session(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    org_id: str | None,
    mfa_satisfied: bool,
    ip_hash: str | None = None,
    user_agent_hash: str | None = None,
    idle_minutes: int = IDLE_TIMEOUT_MINUTES,
    absolute_hours: int = ABSOLUTE_TIMEOUT_HOURS,
) -> tuple[str, str]:
    """Issue a session. Returns (session_id, plaintext token).

    The token is returned to the caller once, to be set as a cookie, and is
    never stored — only its SHA-256 digest is. A fresh session id is minted on
    every login, which is what closes session fixation: an attacker who plants
    a known session value gains nothing, because authentication issues a new
    one rather than elevating the presented one.
    """
    token = new_token()
    now = _now()
    session_id = new_id("SES")
    with conn:
        conn.execute(
            """
            INSERT INTO sessions
                (session_id, token_hash, user_id, org_id, created_at_utc,
                 last_seen_at_utc, absolute_expires_at_utc, idle_expires_at_utc,
                 mfa_satisfied, ip_hash, user_agent_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                hash_token(token),
                user_id,
                org_id,
                now.isoformat(),
                now.isoformat(),
                (now + timedelta(hours=absolute_hours)).isoformat(),
                (now + timedelta(minutes=idle_minutes)).isoformat(),
                1 if mfa_satisfied else 0,
                ip_hash,
                user_agent_hash,
            ),
        )
    return session_id, token


def _row_to_session(row: sqlite3.Row) -> Session:
    return Session(
        session_id=row["session_id"],
        user_id=row["user_id"],
        org_id=row["org_id"],
        mfa_satisfied=bool(row["mfa_satisfied"]),
        created_at=_parse(row["created_at_utc"]),
        absolute_expires_at=_parse(row["absolute_expires_at_utc"]),
        idle_expires_at=_parse(row["idle_expires_at_utc"]),
        revoked_at=_parse(row["revoked_at_utc"]),
    )


def lookup_session(conn: sqlite3.Connection, token: str) -> Session | None:
    """Resolve a cookie value to a live session, or None.

    Looked up by digest, so the presented token is never compared against
    anything stored in plaintext. Expiry and revocation are checked here rather
    than by the caller, because a caller that forgets is a caller that accepts
    an expired session.
    """
    if not token:
        return None
    row = conn.execute(
        "SELECT * FROM sessions WHERE token_hash = ?", (hash_token(token),)
    ).fetchone()
    if row is None:
        return None
    session = _row_to_session(row)
    return session if session.is_live() else None


def touch_session(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    idle_minutes: int = IDLE_TIMEOUT_MINUTES,
) -> None:
    """Push the idle deadline forward. Never touches the absolute deadline."""
    now = _now()
    with conn:
        conn.execute(
            """
            UPDATE sessions SET last_seen_at_utc = ?, idle_expires_at_utc = ?
             WHERE session_id = ? AND revoked_at_utc IS NULL
            """,
            (
                now.isoformat(),
                (now + timedelta(minutes=idle_minutes)).isoformat(),
                session_id,
            ),
        )


def set_session_org(conn: sqlite3.Connection, session_id: str, org_id: str) -> None:
    with conn:
        conn.execute(
            "UPDATE sessions SET org_id = ? WHERE session_id = ?", (org_id, session_id)
        )


def mark_session_mfa_satisfied(conn: sqlite3.Connection, session_id: str) -> None:
    with conn:
        conn.execute(
            "UPDATE sessions SET mfa_satisfied = 1 WHERE session_id = ?", (session_id,)
        )


def revoke_session(conn: sqlite3.Connection, session_id: str) -> None:
    with conn:
        conn.execute(
            "UPDATE sessions SET revoked_at_utc = ? "
            "WHERE session_id = ? AND revoked_at_utc IS NULL",
            (_now().isoformat(), session_id),
        )


def revoke_all_sessions(
    conn: sqlite3.Connection, user_id: str, *, except_session_id: str | None = None
) -> int:
    """Kill every session a user holds. Called on password change and reset.

    A password change that leaves old sessions alive does not evict an
    attacker who already has one, which is the main thing a user changing
    their password after a scare is trying to achieve.
    """
    now = _now().isoformat()
    with conn:
        if except_session_id:
            cursor = conn.execute(
                "UPDATE sessions SET revoked_at_utc = ? "
                "WHERE user_id = ? AND revoked_at_utc IS NULL AND session_id != ?",
                (now, user_id, except_session_id),
            )
        else:
            cursor = conn.execute(
                "UPDATE sessions SET revoked_at_utc = ? "
                "WHERE user_id = ? AND revoked_at_utc IS NULL",
                (now, user_id),
            )
    return cursor.rowcount


def list_user_sessions(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT session_id, created_at_utc, last_seen_at_utc,
               absolute_expires_at_utc, revoked_at_utc
          FROM sessions WHERE user_id = ? ORDER BY created_at_utc DESC LIMIT 50
        """,
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


# --- single-use links -------------------------------------------------------


def issue_auth_token(
    conn: sqlite3.Connection, *, user_id: str, purpose: str, ttl_minutes: int
) -> str:
    """Mint a verification or reset link. Returns the plaintext, stores a digest."""
    token = new_token()
    now = _now()
    with conn:
        conn.execute(
            """
            INSERT INTO auth_tokens
                (token_id, token_hash, purpose, user_id, created_at_utc,
                 expires_at_utc)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("TOK"),
                hash_token(token),
                purpose,
                user_id,
                now.isoformat(),
                (now + timedelta(minutes=ttl_minutes)).isoformat(),
            ),
        )
    return token


def consume_auth_token(
    conn: sqlite3.Connection, *, token: str, purpose: str
) -> str | None:
    """Redeem a link exactly once. Returns the user id, or None.

    Single use is enforced by the `consumed_at_utc IS NULL` guard inside the
    UPDATE, so two simultaneous redemptions of one reset link cannot both
    succeed — the second changes no rows and is refused.
    """
    if not token:
        return None
    digest = hash_token(token)
    now = _now()
    row = conn.execute(
        "SELECT * FROM auth_tokens WHERE token_hash = ? AND purpose = ?",
        (digest, purpose),
    ).fetchone()
    if row is None or row["consumed_at_utc"] is not None:
        return None
    expires_at = _parse(row["expires_at_utc"])
    if expires_at is None or now >= expires_at:
        return None

    with conn:
        cursor = conn.execute(
            """
            UPDATE auth_tokens SET consumed_at_utc = ?
             WHERE token_hash = ? AND consumed_at_utc IS NULL
            """,
            (now.isoformat(), digest),
        )
    return row["user_id"] if cursor.rowcount == 1 else None


def invalidate_tokens(conn: sqlite3.Connection, *, user_id: str, purpose: str) -> None:
    """Burn every outstanding link of one kind for a user.

    Called when a reset completes, so a second reset email requested earlier
    cannot still be used afterwards.
    """
    with conn:
        conn.execute(
            """
            UPDATE auth_tokens SET consumed_at_utc = ?
             WHERE user_id = ? AND purpose = ? AND consumed_at_utc IS NULL
            """,
            (_now().isoformat(), user_id, purpose),
        )


# --- conversations ----------------------------------------------------------
#
# A conversation id is supplied by the client on every chat turn, and prepared
# actions are bound to it. Without an ownership record, anyone who learned or
# guessed a conversation id could satisfy the action's session binding. These
# three functions make the id a *claim about a row this user owns* rather than
# a string the request asserts.


def claim_conversation(
    conn: sqlite3.Connection,
    *,
    conversation_id: str,
    user_id: str,
    org_id: str | None,
) -> bool:
    """Register or re-assert ownership of a conversation. False if it is someone else's.

    First use registers it; later uses check it. The INSERT carries the
    ownership guard itself (`INSERT OR IGNORE` then a scoped read) so two
    simultaneous first-uses cannot both claim the same id.
    """
    now = _now().isoformat()
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO conversations
                (conversation_id, user_id, org_id, created_at_utc, last_used_at_utc)
            VALUES (?, ?, ?, ?, ?)
            """,
            (conversation_id, user_id, org_id, now, now),
        )
    row = conn.execute(
        "SELECT user_id, org_id FROM conversations WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    if row is None or row["user_id"] != user_id:
        return False
    # A conversation also cannot be carried across tenants: the same person
    # switching organisation starts a new conversation rather than continuing
    # one whose evidence came from a different tenant's data.
    if row["org_id"] != org_id:
        return False
    with conn:
        conn.execute(
            "UPDATE conversations SET last_used_at_utc = ? WHERE conversation_id = ?",
            (now, conversation_id),
        )
    return True


def conversation_owner(
    conn: sqlite3.Connection, conversation_id: str
) -> tuple[str, str | None] | None:
    row = conn.execute(
        "SELECT user_id, org_id FROM conversations WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    return None if row is None else (row["user_id"], row["org_id"])
