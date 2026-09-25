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
    -- How a workspace is joined: a code the server generated and a password its
    -- owner chose. Its own table rather than columns on `organizations`, so no
    -- `SELECT * FROM organizations` -- and there are several -- can carry the
    -- hash into a response. A workspace with no row here (the seeded demo, one
    -- created by a script) simply cannot be joined by code.
    CREATE TABLE IF NOT EXISTS workspace_access (
        org_id TEXT PRIMARY KEY REFERENCES organizations (org_id),
        -- Unique at the database, not only in the generator: two workspaces
        -- with one code would let a password for one open the other.
        workspace_code TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        owner_user_id TEXT NOT NULL REFERENCES users (user_id),
        created_at_utc TEXT NOT NULL,
        password_changed_at_utc TEXT NOT NULL
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
    # --- invitations --------------------------------------------------------
    #
    # An invitation is a credential: whoever holds the token can join a
    # workspace. It is therefore stored exactly like a session or a reset link
    # -- only the SHA-256 digest, never the token itself. A database
    # disclosure yields no usable invitation.
    #
    # `email` is the address the invitation was *issued to*, normalised. It is
    # checked at acceptance against the authenticated user's own address, so a
    # leaked link cannot be redeemed by whoever happens to find it.
    """
    CREATE TABLE IF NOT EXISTS invitations (
        invitation_id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations (org_id),
        email TEXT NOT NULL,
        role TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        invited_by TEXT NOT NULL REFERENCES users (user_id),
        created_at_utc TEXT NOT NULL,
        expires_at_utc TEXT NOT NULL,
        -- Set on redemption. Single-use is enforced by a guarded UPDATE on
        -- this column rather than by deleting the row, so a replayed link is
        -- observable rather than merely absent.
        accepted_at_utc TEXT,
        accepted_by TEXT REFERENCES users (user_id),
        revoked_at_utc TEXT,
        revoked_by TEXT REFERENCES users (user_id)
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
    # --- email verification by one-time code ------------------------------
    #
    # A *verification* is a browser's claim to be proving one address. It is
    # held as an HttpOnly cookie whose SHA-256 digest is `token_hash`, exactly
    # like a session. It is issued only to a caller who created the account or
    # who has just presented its correct password — so the verify endpoints
    # take no email address and cannot be used to probe which addresses exist.
    #
    # `decoy` marks the verification handed back when someone registers an
    # address that is already taken. It behaves like a real one in every
    # observable way (codes, expiry, attempts, cooldowns) and can never
    # succeed; that is what keeps registration from being an existence oracle.
    #
    # `purpose = 'oauth_signup'` rows carry a Google/GitHub identity whose
    # provider supplied no verified address; `email` is NULL until the person
    # types one, and the code proves it.
    """
    CREATE TABLE IF NOT EXISTS email_verifications (
        verification_id TEXT PRIMARY KEY,
        token_hash TEXT NOT NULL UNIQUE,
        purpose TEXT NOT NULL,
        user_id TEXT REFERENCES users (user_id),
        email TEXT,
        decoy INTEGER NOT NULL DEFAULT 0,
        provider TEXT,
        provider_subject TEXT,
        provider_display_name TEXT,
        created_at_utc TEXT NOT NULL,
        expires_at_utc TEXT NOT NULL,
        -- Set once, under a guarded UPDATE: a verification completes once.
        completed_at_utc TEXT
    ) STRICT
    """,
    # One row per code sent. The code itself is never stored: `code_hash` is
    # HMAC-SHA256 keyed by a per-row random salt over the verification id and
    # the code, so a stored hash is useless for any other verification and
    # cannot be looked up in a precomputed table.
    """
    CREATE TABLE IF NOT EXISTS email_otps (
        otp_id TEXT PRIMARY KEY,
        verification_id TEXT NOT NULL REFERENCES email_verifications (verification_id),
        -- The destination, for the per-address sending cap. NULL on a decoy,
        -- so a decoy can never use up the real owner's allowance.
        email TEXT,
        code_salt TEXT NOT NULL,
        code_hash TEXT NOT NULL,
        created_at_utc TEXT NOT NULL,
        expires_at_utc TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        delivered INTEGER NOT NULL DEFAULT 0,
        consumed_at_utc TEXT,
        -- Set when a newer code replaces this one: only the latest code works.
        superseded_at_utc TEXT
    ) STRICT
    """,
    # --- sign-in with Google / GitHub ----------------------------------------
    #
    # A user may hold any number of provider identities. (provider, subject)
    # is unique, so one Google account can never be attached to two ASTRION
    # users. The provider's access token is never stored: it is used once, to
    # read the profile, and dropped.
    """
    CREATE TABLE IF NOT EXISTS user_identities (
        identity_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users (user_id),
        provider TEXT NOT NULL,
        provider_subject TEXT NOT NULL,
        email_at_link TEXT,
        created_at_utc TEXT NOT NULL,
        last_used_at_utc TEXT,
        UNIQUE (provider, provider_subject)
    ) STRICT
    """,
    # The `state` of an authorization request in flight: its digest, the PKCE
    # verifier that goes with it, and single use. The same value is also held
    # in an HttpOnly cookie on the browser that started the flow, and the
    # callback requires the two to match — which is what stops someone
    # completing a sign-in in another person's browser.
    """
    CREATE TABLE IF NOT EXISTS oauth_states (
        state_hash TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        code_verifier TEXT NOT NULL,
        created_at_utc TEXT NOT NULL,
        expires_at_utc TEXT NOT NULL,
        consumed_at_utc TEXT
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_email_verifications_user "
    "ON email_verifications (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_email_otps_verification "
    "ON email_otps (verification_id, created_at_utc)",
    "CREATE INDEX IF NOT EXISTS idx_email_otps_email ON email_otps (email, created_at_utc)",
    "CREATE INDEX IF NOT EXISTS idx_user_identities_user ON user_identities (user_id)",
    # At most ONE account may belong to ONE workspace. Without this, the same
    # dataset account could be granted to two workspaces and each would see the
    # other's orders, tickets and actions -- the composite primary key on
    # (org_id, account_id) permits exactly that. This index is the tenant
    # boundary expressed as a constraint rather than as a convention.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_organization_accounts_account "
    "ON organization_accounts (account_id)",
    # One *outstanding* invitation per address per workspace. Partial, so a
    # spent or revoked invitation does not block re-inviting someone, while two
    # simultaneous invites to the same address cannot both be created.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_invitations_pending "
    "ON invitations (org_id, email) "
    "WHERE accepted_at_utc IS NULL AND revoked_at_utc IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_invitations_org ON invitations (org_id)",
    "CREATE INDEX IF NOT EXISTS idx_invitations_email ON invitations (email)",
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
