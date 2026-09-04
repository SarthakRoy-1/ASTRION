"""The security audit trail: append-only, hash-chained, and redacting.

Every entry commits to the one before it::

    entry_hash = SHA256(prev_hash || canonical-json(entry fields))

So the log is *tamper-evident*. Editing a row, deleting one, or reordering two
breaks the chain from that point on, and `verify_audit_chain` reports the exact
sequence number where the break starts. It is not tamper-proof: anyone with
write access to the database can recompute the entire chain forward. Making it
tamper-proof requires a witness outside this box — an append-only log service,
or periodic publication of the head hash — which docs/SECURITY.md names as a
limitation rather than this module pretending to provide.

**What must never reach this table.** An audit log is read by more people, kept
longer, and exported more often than the data it describes, so it is exactly
the wrong place for a secret. `_redact` drops any detail key that names a
credential, and truncates long values, before an entry is written — a
belt-and-braces control on top of callers being careful, because the caller
that forgets is the one that matters.

Failures are recorded, not just successes. An authorization refusal is the
event a reviewer most wants and the one a naive implementation omits, because
the code path that denies usually returns early.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from app.backend.auth.tokens import new_id

logger = logging.getLogger("parcelpilot.audit")

#: The chain's anchor. The first entry commits to this constant, so an
#: attacker cannot truncate the log to nothing and claim it was always empty:
#: an empty log and a log whose head is this value are distinguishable.
GENESIS_HASH = "0" * 64


class AuditEvent(StrEnum):
    """Security-relevant events. Closed vocabulary so the log is queryable.

    Adding a member here is how a new security-relevant operation becomes
    reviewable; a free-text event name would make the log unsearchable within
    a month.
    """

    # Identity
    LOGIN_SUCCEEDED = "login.succeeded"
    LOGIN_FAILED = "login.failed"
    LOGIN_LOCKED_OUT = "login.locked_out"
    LOGOUT = "logout"
    REGISTERED = "user.registered"
    EMAIL_VERIFIED = "user.email_verified"
    PASSWORD_RESET_REQUESTED = "user.password_reset_requested"
    PASSWORD_RESET_COMPLETED = "user.password_reset_completed"
    PASSWORD_CHANGED = "user.password_changed"
    MFA_ENROLMENT_STARTED = "mfa.enrolment_started"
    MFA_ENABLED = "mfa.enabled"
    MFA_DISABLED = "mfa.disabled"
    MFA_CHALLENGE_FAILED = "mfa.challenge_failed"
    SESSION_REVOKED = "session.revoked"

    # Tenancy. The event names keep the `org.` prefix that Phase 0 wrote, so
    # existing entries stay queryable alongside new ones; `workspace` is the
    # product term for the same entity.
    ORGANIZATION_CREATED = "org.created"
    ORGANIZATION_UPDATED = "org.updated"
    MEMBERSHIP_CREATED = "org.membership_created"
    MEMBERSHIP_ROLE_CHANGED = "org.membership_role_changed"
    MEMBERSHIP_REMOVED = "org.membership_removed"
    OWNERSHIP_TRANSFERRED = "org.ownership_transferred"
    WORKSPACE_ACTIVATED = "org.workspace_activated"

    # Invitations. The token never appears in any of these — only the
    # invitation id, the role, and the invited address's domain.
    INVITATION_CREATED = "invitation.created"
    INVITATION_ACCEPTED = "invitation.accepted"
    INVITATION_ACCEPT_FAILED = "invitation.accept_failed"
    INVITATION_REVOKED = "invitation.revoked"

    # Authorization
    AUTHORIZATION_DENIED = "authz.denied"
    TENANT_ISOLATION_DENIED = "authz.tenant_denied"

    # Agent and actions
    AGENT_INVOKED = "agent.invoked"
    ACTION_PROPOSED = "action.proposed"
    ACTION_EXECUTED = "action.executed"
    ACTION_REJECTED = "action.rejected"
    ACTION_CONFIRMATION_REFUSED = "action.confirmation_refused"

    # Abuse controls
    RATE_LIMITED = "abuse.rate_limited"
    PAYLOAD_REJECTED = "abuse.payload_rejected"


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"


#: Detail keys that are dropped rather than written. Matched as substrings and
#: case-insensitively, so `reset_token`, `X-Api-Key` and `newPassword` are all
#: caught without needing to enumerate every spelling a caller might invent.
_REDACT_SUBSTRINGS = (
    "password",
    "secret",
    # Catches `token`, `session_token`, `reset_token` and `invitation_token`
    # alike — substring matching is what makes this hold for spellings nobody
    # thought to enumerate.
    "token",
    "authorization",
    "cookie",
    "api_key",
    "apikey",
    "credential",
    "mfa_code",
    "otp",
)

#: Cap on any single stringified detail value. An audit entry is metadata; a
#: caller that passes a whole document body is making the log unreadable and
#: potentially copying customer data into it.
_MAX_DETAIL_CHARS = 500

REDACTED = "[redacted]"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hash_identifier(value: str | None) -> str | None:
    """A stable, non-reversible handle for an IP or an email.

    Lets the log answer "was this the same client" and "how many failures from
    one source" without storing the address. Unsalted on purpose: a salt held
    in the same database protects nothing, and the value is a correlation
    handle rather than a secret.
    """
    if not value:
        return None
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()[:32]


def _redact(details: Mapping[str, Any] | None) -> dict[str, Any]:
    """Strip credentials and oversized values before anything is persisted."""
    if not details:
        return {}
    clean: dict[str, Any] = {}
    for key, value in details.items():
        name = str(key)
        if any(marker in name.lower() for marker in _REDACT_SUBSTRINGS):
            clean[name] = REDACTED
            continue
        if isinstance(value, str) and len(value) > _MAX_DETAIL_CHARS:
            clean[name] = value[:_MAX_DETAIL_CHARS] + "…"
        elif isinstance(value, (str, int, float, bool)) or value is None:
            clean[name] = value
        elif isinstance(value, (list, tuple)):
            clean[name] = [str(v)[:_MAX_DETAIL_CHARS] for v in value][:50]
        elif isinstance(value, Mapping):
            clean[name] = _redact(value)
        else:
            clean[name] = str(value)[:_MAX_DETAIL_CHARS]
    return clean


def _head_hash(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    return GENESIS_HASH if row is None else row["entry_hash"]


def _compute_hash(prev_hash: str, payload: Mapping[str, Any]) -> str:
    """Commit to the predecessor and to every field of this entry.

    `sort_keys` makes the serialisation canonical: the same entry must hash
    identically on the machine that wrote it and the machine that audits it,
    whatever order the dict happened to be built in.
    """
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{prev_hash}{body}".encode("utf-8")).hexdigest()


def record_event(
    conn: sqlite3.Connection,
    event_type: AuditEvent,
    *,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    actor_user_id: str | None = None,
    actor_role: str | None = None,
    org_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    request_id: str | None = None,
    ip_hash: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> str:
    """Append one entry and return its id.

    **Never raises.** Audit logging is an observer: a failure to record an
    event must not turn a successful login into a 500, nor — far worse — undo
    a security check by aborting the transaction it runs inside. A write that
    fails is reported to the application log and the request continues.
    """
    try:
        occurred_at = _now().isoformat()
        event_id = new_id("AUD")
        payload = {
            "event_id": event_id,
            "occurred_at_utc": occurred_at,
            "event_type": event_type.value,
            "outcome": outcome.value,
            "actor_user_id": actor_user_id,
            "actor_role": actor_role,
            "org_id": org_id,
            "target_type": target_type,
            "target_id": target_id,
            "request_id": request_id,
            "ip_hash": ip_hash,
            "details": _redact(details),
        }
        detail_json = json.dumps(payload["details"], sort_keys=True, default=str)

        # Read the head and append in one transaction so two concurrent writers
        # cannot both chain from the same predecessor and fork the log.
        with conn:
            prev_hash = _head_hash(conn)
            entry_hash = _compute_hash(prev_hash, payload)
            conn.execute(
                """
                INSERT INTO audit_log
                    (event_id, occurred_at_utc, event_type, outcome, actor_user_id,
                     actor_role, org_id, target_type, target_id, request_id, ip_hash,
                     detail_json, prev_hash, entry_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    occurred_at,
                    event_type.value,
                    outcome.value,
                    actor_user_id,
                    actor_role,
                    org_id,
                    target_type,
                    target_id,
                    request_id,
                    ip_hash,
                    detail_json,
                    prev_hash,
                    entry_hash,
                ),
            )
        return event_id
    except Exception:  # pragma: no cover - defensive
        logger.exception("failed to record audit event %s", event_type)
        return ""


def verify_audit_chain(conn: sqlite3.Connection) -> tuple[bool, int | None]:
    """Recompute the chain. Returns (intact, first bad seq).

    Run this on a schedule or before exporting the log. A `False` result means
    a row was altered, removed or inserted out of band — not necessarily an
    attack, but always something that needs explaining.
    """
    prev_hash = GENESIS_HASH
    for row in conn.execute("SELECT * FROM audit_log ORDER BY seq"):
        payload = {
            "event_id": row["event_id"],
            "occurred_at_utc": row["occurred_at_utc"],
            "event_type": row["event_type"],
            "outcome": row["outcome"],
            "actor_user_id": row["actor_user_id"],
            "actor_role": row["actor_role"],
            "org_id": row["org_id"],
            "target_type": row["target_type"],
            "target_id": row["target_id"],
            "request_id": row["request_id"],
            "ip_hash": row["ip_hash"],
            "details": json.loads(row["detail_json"]),
        }
        if row["prev_hash"] != prev_hash:
            return False, int(row["seq"])
        if _compute_hash(prev_hash, payload) != row["entry_hash"]:
            return False, int(row["seq"])
        prev_hash = row["entry_hash"]
    return True, None


def list_events(
    conn: sqlite3.Connection,
    *,
    org_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Read the log, optionally for one organisation.

    `org_id` is *not* optional in practice — the route that exposes this
    passes the caller's own organisation, derived from their session. Leaving
    it None reads across tenants and is for operator tooling only.
    """
    limit = max(1, min(int(limit), 1000))
    if org_id is None:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY seq DESC LIMIT ?", (limit,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE org_id = ? ORDER BY seq DESC LIMIT ?",
            (org_id, limit),
        ).fetchall()
    return [
        {
            "seq": row["seq"],
            "event_id": row["event_id"],
            "occurred_at_utc": row["occurred_at_utc"],
            "event_type": row["event_type"],
            "outcome": row["outcome"],
            "actor_user_id": row["actor_user_id"],
            "actor_role": row["actor_role"],
            "org_id": row["org_id"],
            "target_type": row["target_type"],
            "target_id": row["target_id"],
            "request_id": row["request_id"],
            "details": json.loads(row["detail_json"]),
        }
        for row in rows
    ]
