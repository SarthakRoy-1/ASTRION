"""Schema for identity, tenancy, and the security audit trail.

Kept separate from `services/database.py` because the two have different
lifecycles. The dataset tables there are *regenerable*:
`scripts/ingest_dataset.py` deletes and rewrites them from the workbook on
every run. The tables here hold the only copy of their data — a user, an
organisation, an executed confirmation, an audit entry — and no ingestion run
may ever touch them.

Shape of the tenancy model::

    users --< memberships >-- organizations --< organization_accounts
                  |                                       |
                  role                       the dataset account ids
                                             this organisation owns

`organization_accounts` is the join that makes the whole existing enforcement
path work unchanged. Every query below the tool layer already filters on
`allowed_account_ids`; membership now *derives* that set server-side instead
of a mock directory supplying it. Nothing downstream had to change, which is
precisely why the boundary was worth keeping where it was.

Note what is absent: no table stores a password, a session token, a reset link
or a TOTP code in a replayable form. Passwords are scrypt hashes; tokens are
SHA-256 digests of secrets shown exactly once.

Also absent, deliberately: an idempotency-key table. Single-use execution is
already a property of `agent_actions` — `execute_action` transitions the row
under a `WHERE status = 'pending_confirmation'` guard, so a replayed
confirmation changes no rows and is refused. A second mechanism would be a
second source of truth about whether an action had run.
"""

from __future__ import annotations

SECURITY_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        -- Stored lower-cased and stripped. UNIQUE here is what makes
        -- registration safe against a read-then-write race that would
        -- otherwise create two accounts for one address.
        email TEXT NOT NULL UNIQUE,
        email_verified INTEGER NOT NULL DEFAULT 0,
        display_name TEXT NOT NULL,
        -- scrypt hash, self-describing. See app/backend/auth/passwords.py
        password_hash TEXT NOT NULL,
        password_updated_at_utc TEXT NOT NULL,
        -- Base32 TOTP secret. NULL until enrolment; mfa_enabled stays 0 until
        -- a first code is verified, so a half-finished enrolment cannot lock a
        -- user out of their own account.
        mfa_secret TEXT,
        mfa_enabled INTEGER NOT NULL DEFAULT 0,
        -- The last TOTP time-step spent, so a code cannot be replayed inside
        -- its own acceptance window.
        mfa_last_step INTEGER,
        status TEXT NOT NULL DEFAULT 'active',
        created_at_utc TEXT NOT NULL,
        last_login_at_utc TEXT
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS organizations (
        org_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        slug TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'active',
        created_at_utc TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS memberships (
        membership_id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations (org_id),
        user_id TEXT NOT NULL REFERENCES users (user_id),
        role TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at_utc TEXT NOT NULL,
        updated_at_utc TEXT NOT NULL,
        -- One membership per user per organisation. Without this, a second
        -- INSERT would silently grant a second, possibly higher, role.
        UNIQUE (org_id, user_id)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS organization_accounts (
        org_id TEXT NOT NULL REFERENCES organizations (org_id),
        account_id TEXT NOT NULL,
        created_at_utc TEXT NOT NULL,
        PRIMARY KEY (org_id, account_id)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        -- SHA-256 of the cookie value. The cookie itself is never stored, so
        -- a database disclosure yields no usable session.
        token_hash TEXT NOT NULL UNIQUE,
        user_id TEXT NOT NULL REFERENCES users (user_id),
        -- The organisation this session is acting in. Every tenant-scoped
        -- query derives its scope from this column, never from the request.
        org_id TEXT REFERENCES organizations (org_id),
        created_at_utc TEXT NOT NULL,
        last_seen_at_utc TEXT NOT NULL,
        -- Two independent clocks: a session dies at the absolute deadline
        -- however active it has been, and dies early if left idle.
        absolute_expires_at_utc TEXT NOT NULL,
        idle_expires_at_utc TEXT NOT NULL,
        revoked_at_utc TEXT,
        -- 0 between password and second factor. A half-authenticated session
        -- can reach the MFA endpoint and nothing else.
        mfa_satisfied INTEGER NOT NULL DEFAULT 0,
        ip_hash TEXT,
        user_agent_hash TEXT
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS auth_tokens (
        token_id TEXT PRIMARY KEY,
        token_hash TEXT NOT NULL UNIQUE,
        purpose TEXT NOT NULL,
        user_id TEXT NOT NULL REFERENCES users (user_id),
        created_at_utc TEXT NOT NULL,
        expires_at_utc TEXT NOT NULL,
        -- Set on first use. Single-use is enforced by a guarded UPDATE on this
        -- column rather than by deleting the row, so a reused link is
        -- observable rather than merely absent.
        consumed_at_utc TEXT
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS login_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        -- A hash of the email or the client address, never the value itself:
        -- this table exists to count failures, not to accumulate a record of
        -- who tried to log in from where.
        identifier_hash TEXT NOT NULL,
        scope TEXT NOT NULL,
        attempted_at_utc TEXT NOT NULL,
        successful INTEGER NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS conversations (
        conversation_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users (user_id),
        org_id TEXT REFERENCES organizations (org_id),
        created_at_utc TEXT NOT NULL,
        last_used_at_utc TEXT NOT NULL
    ) STRICT
    """,
    # --- audit trail --------------------------------------------------------
    #
    # Append-only and hash-chained: each entry commits to its predecessor, so
    # deleting or editing any row breaks every hash after it and
    # `verify_audit_chain` reports the exact position. This is tamper-EVIDENT,
    # not tamper-proof: an attacker holding write access can recompute the
    # whole chain. Tamper-proof needs an off-box witness, which is named as a
    # limitation in docs/SECURITY.md rather than pretended at here.
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        occurred_at_utc TEXT NOT NULL,
        event_type TEXT NOT NULL,
        outcome TEXT NOT NULL,
        actor_user_id TEXT,
        actor_role TEXT,
        org_id TEXT,
        target_type TEXT,
        target_id TEXT,
        request_id TEXT,
        ip_hash TEXT,
        detail_json TEXT NOT NULL,
        prev_hash TEXT NOT NULL,
        entry_hash TEXT NOT NULL
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_memberships_user ON memberships (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_memberships_org ON memberships (org_id)",
    "CREATE INDEX IF NOT EXISTS idx_org_accounts_account "
    "ON organization_accounts (account_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_auth_tokens_user ON auth_tokens (user_id, purpose)",
    "CREATE INDEX IF NOT EXISTS idx_login_attempts_lookup "
    "ON login_attempts (identifier_hash, attempted_at_utc)",
    "CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log (actor_user_id, occurred_at_utc)",
    "CREATE INDEX IF NOT EXISTS idx_audit_org ON audit_log (org_id, occurred_at_utc)",
    "CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_log (event_type, occurred_at_utc)",
)
