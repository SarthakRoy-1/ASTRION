"""Resolving *who is calling*, and the authority they hold.

This module is the trust boundary. Above it, a request is a bag of bytes a
stranger sent; below it, every layer already written assumes an `AgentContext`
whose `allowed_account_ids` can be believed. Getting that transition right is
the whole job, and it comes down to one rule:

    Nothing in the request contributes to the caller's authority.

The session cookie is the *only* thing read from the request, and all it does
is name a row. Identity, organisation, role, permissions and tenant scope are
then read from the database. A body field called `user_id`, a header naming a
role, an `org_id` in a path — none of them is consulted. The narrowing
`account_scope` field is the single exception, and it can only intersect, never
add (see `AuthenticatedCaller.agent_context`).

`AuthMode.DEMO_HEADER` reproduces the original assessment behaviour, where the
client asserts an identity and the server believes it. That mode is
authentication in name only and is refused in production by
`Settings.validate_auth`. It lives here, beside the real path, so the
difference between the two is one branch a reviewer can read rather than a
claim in a document.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from fastapi import Request

from app.backend.auth import repository as repo
from app.backend.auth.permissions import Permission, permissions_for
from app.backend.auth.principals import MOCK_PRINCIPALS, get_principal
from app.backend.core.config import AuthMode, Settings
from app.backend.core.errors import AuthenticationError, AuthorizationError
from app.backend.models.agent import AgentContext, Role
from app.backend.services.audit import (
    AuditEvent,
    AuditOutcome,
    hash_identifier,
    record_event,
)

#: How an organisation role is reported through the existing `Role` field on
#: `AgentContext`. The mapping exists only so the wire contract and the
#: deterministic planner keep seeing a vocabulary they already understand;
#: **no authorization decision reads it**. Every such decision reads the
#: permission set, which is carried alongside and is authoritative.
_ROLE_PROJECTION: dict[str, Role] = {
    "owner": Role.SUPPORT_MANAGER,
    "admin": Role.SUPPORT_MANAGER,
    "operations": Role.SUPPORT_AGENT,
    "support": Role.SUPPORT_AGENT,
    "viewer": Role.READ_ONLY,
}


@dataclass(frozen=True)
class AuthenticatedCaller:
    """A verified caller, and everything the application may believe about them.

    Built only by `authenticate`. Every field here came from the database, not
    from the request — which is what lets the rest of the application treat it
    as fact.
    """

    user_id: str
    display_name: str
    org_id: str | None
    org_name: str | None
    role: Role
    org_role: str | None
    permissions: frozenset[Permission]
    allowed_account_ids: frozenset[str] | None
    auth_session_id: str | None
    mfa_satisfied: bool
    is_demo: bool = False

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions

    def require(self, permission: Permission) -> None:
        """Raise unless the caller holds `permission`.

        The message names the permission rather than the role, because "you
        need execute_action" tells an operator what to grant and "your role is
        support" leaves them guessing.
        """
        if not self.has(permission):
            raise AuthorizationError(
                f"This action requires the {permission.value!r} permission, "
                f"which your role does not grant."
            )

    def agent_context(
        self,
        *,
        session_id: str | None = None,
        account_scope: list[str] | None = None,
    ) -> AgentContext:
        """The authorization envelope handed to the agent.

        `account_scope` may only *narrow*: it is intersected with the scope
        this caller already holds. A client asking for an account it does not
        own gets an unchanged scope and, below this layer, no data — the same
        guarantee `auth/principals.py` established, now sourced from
        organisation membership instead of a fixed directory.
        """
        scope = self.allowed_account_ids
        if account_scope is not None:
            requested = {str(a).strip() for a in account_scope if str(a).strip()}
            scope = (
                frozenset(requested)
                if scope is None
                else frozenset(scope) & requested
            )

        return AgentContext(
            user_id=self.user_id,
            role=self.role,
            allowed_account_ids=scope,
            session_id=session_id,
            org_id=self.org_id,
            # `None` for the demo path, so `AgentContext.may_change_state`
            # falls back to its original role mapping and Phase 4 behaviour is
            # preserved exactly.
            permissions=(
                frozenset(p.value for p in self.permissions)
                if not self.is_demo
                else None
            ),
        )


def _session_token(request: Request, settings: Settings) -> str | None:
    """Read the session cookie.

    Cookie only — never a query parameter and never a bearer header. A token in
    a URL ends up in browser history, in `Referer`, and in every access log
    between here and the client.
    """
    return request.cookies.get(settings.session_cookie_name)


def authenticate(
    request: Request,
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    allow_pending_mfa: bool = False,
) -> AuthenticatedCaller:
    """Establish the caller, or refuse. The only entry point routes should use.

    `allow_pending_mfa` is true for exactly one endpoint — the second-factor
    challenge — because a session that has passed a password and not yet a code
    must be able to reach that and nothing else.
    """
    if settings.auth_mode is AuthMode.DEMO_HEADER:
        return _authenticate_demo(request, conn)
    return _authenticate_session(
        request, conn, settings, allow_pending_mfa=allow_pending_mfa
    )


def _authenticate_session(
    request: Request,
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    allow_pending_mfa: bool,
) -> AuthenticatedCaller:
    token = _session_token(request, settings)
    if not token:
        raise AuthenticationError("Authentication is required.")

    session = repo.lookup_session(conn, token)
    if session is None:
        # Covers unknown, expired and revoked alike. Distinguishing them would
        # tell a holder of a stale token which kind of stale it is, and there is
        # nothing a legitimate client does differently in response.
        raise AuthenticationError("Your session has expired. Sign in again.")

    if not session.mfa_satisfied and not allow_pending_mfa:
        raise AuthenticationError(
            "Two-factor authentication has not been completed for this session.",
            details={"mfa_required": True},
        )

    user = repo.get_user(conn, session.user_id)
    if user is None or not user.is_active:
        # The account was disabled after the session was issued. Revoke rather
        # than merely refuse, so the cookie stops working everywhere at once.
        repo.revoke_session(conn, session.session_id)
        raise AuthenticationError("Your session is no longer valid.")

    # Sliding idle window. Only ever extends the idle deadline; the absolute
    # deadline set at login is untouched, so an actively-used stolen cookie
    # still dies on schedule.
    repo.touch_session(conn, session.session_id)
    request.state.auth_user_id = user.user_id

    org_id = session.org_id
    membership = None
    if org_id:
        membership = repo.get_membership(conn, org_id=org_id, user_id=user.user_id)
        if membership is None:
            # Membership was revoked while the session lived. The session stays
            # valid as an identity; it simply carries no tenant authority.
            record_event(
                conn,
                AuditEvent.TENANT_ISOLATION_DENIED,
                outcome=AuditOutcome.DENIED,
                actor_user_id=user.user_id,
                org_id=org_id,
                details={"reason": "membership_revoked_during_session"},
            )
            org_id = None

    if membership is None:
        return AuthenticatedCaller(
            user_id=user.user_id,
            display_name=user.display_name,
            org_id=None,
            org_name=None,
            role=Role.READ_ONLY,
            org_role=None,
            permissions=frozenset(),
            # Authorized for no customer-specific data at all. An empty set is
            # emphatically not `None`, which below this layer means unrestricted.
            allowed_account_ids=frozenset(),
            auth_session_id=session.session_id,
            mfa_satisfied=session.mfa_satisfied,
        )

    return AuthenticatedCaller(
        user_id=user.user_id,
        display_name=user.display_name,
        org_id=membership.org_id,
        org_name=membership.org_name,
        role=_ROLE_PROJECTION.get(membership.role.value, Role.READ_ONLY),
        org_role=membership.role.value,
        permissions=permissions_for(membership.role),
        # The tenant boundary, derived from the organisation on the session.
        allowed_account_ids=repo.accounts_for_org(conn, membership.org_id),
        auth_session_id=session.session_id,
        mfa_satisfied=session.mfa_satisfied,
    )


def _authenticate_demo(
    request: Request, conn: sqlite3.Connection
) -> AuthenticatedCaller:
    """The original mock: believe whatever identity the client claims.

    Retained so the demo and the agent test-suite can act as a named persona
    without a login, and reachable only when `AUTH_MODE=demo_header` — which
    `Settings.validate_auth` refuses to accept in production.
    """
    header = request.headers.get("x-astrion-user")
    body_user = getattr(request.state, "body_user_id", None)
    principal = get_principal(header or body_user)

    request.state.auth_user_id = principal.user_id
    return AuthenticatedCaller(
        user_id=principal.user_id,
        display_name=principal.display_name,
        org_id=None,
        org_name=None,
        role=principal.role,
        org_role=None,
        # Empty rather than populated: the demo path deliberately produces a
        # context with `permissions=None`, so `may_change_state` uses the
        # original role mapping and Phase 4 behaviour is bit-for-bit preserved.
        permissions=frozenset(),
        allowed_account_ids=principal.resolve_scope(conn),
        auth_session_id=None,
        mfa_satisfied=True,
        is_demo=True,
    )


def demo_identities_available(settings: Settings) -> bool:
    """Whether `GET /api/principals` should list anything.

    In session mode the mock directory is not an identity source, and
    publishing it would advertise account names that no longer mean anything.
    """
    return settings.auth_mode is AuthMode.DEMO_HEADER and bool(MOCK_PRINCIPALS)


def audit_denial(
    conn: sqlite3.Connection,
    caller: AuthenticatedCaller | None,
    *,
    permission: Permission | None = None,
    request_id: str | None = None,
    client_ip: str | None = None,
    detail: str | None = None,
) -> None:
    """Record an authorization refusal.

    Denials are the entries a reviewer most wants and the ones an
    implementation most often omits, because the denying branch returns early.
    """
    record_event(
        conn,
        AuditEvent.AUTHORIZATION_DENIED,
        outcome=AuditOutcome.DENIED,
        actor_user_id=caller.user_id if caller else None,
        actor_role=caller.org_role if caller else None,
        org_id=caller.org_id if caller else None,
        request_id=request_id,
        ip_hash=hash_identifier(client_ip),
        details={
            "permission": permission.value if permission else None,
            "detail": detail,
        },
    )
