"""The authentication endpoints.

Registration, verification, login, second factor, logout, password reset and
change, organisation membership, and the audit log. Everything that issues or
consumes a credential lives here so the credential-handling surface is one
file rather than a property of the whole API.

Three conventions hold throughout, and each is a security control rather than
a style:

- **The session token never appears in a response body.** It is set as an
  `HttpOnly` cookie and nowhere else, so no amount of XSS on the frontend can
  read it and no logging middleware can capture it from a payload.
- **Enumeration-safe endpoints return one shape.** Register and
  password-reset-request answer identically whether or not the address exists.
  The route has nothing to branch on, because the service layer already
  refused to tell it.
- **Delivery is out of scope, and says so.** This deployment has no mail
  sender. Verification and reset links are returned in the response *only*
  when the deployment is not production, gated by `_may_disclose_link`. In
  production they are withheld and logged for an operator, because a reset
  link in an API response is a reset link anyone who can call the endpoint can
  have.
"""

from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.backend.api.authentication import authenticate
from app.backend.api.dependencies import DbDep
from app.backend.api.ratelimit import client_address
from app.backend.auth import repository as repo
from app.backend.auth import service as auth_service
from app.backend.auth.permissions import OrgRole, Permission
from app.backend.core.config import AuthMode, Settings
from app.backend.core.errors import (
    AuthenticationError,
    AuthorizationError,
    InvalidRequestError,
    NotFoundError,
)
from app.backend.services.audit import (
    AuditEvent,
    hash_identifier,
    list_events,
    record_event,
    verify_audit_chain,
)

logger = logging.getLogger("parcelpilot.auth")

auth_router = APIRouter(prefix="/api/auth", tags=["auth"])

#: Bounds on every free-text field an unauthenticated caller can post. Without
#: these, `max_request_bytes` is the only ceiling and a single field could
#: carry a quarter-megabyte into scrypt or into the audit log.
MAX_EMAIL = 320
MAX_NAME = 200
MAX_PASSWORD = 1024


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _may_disclose_link(settings: Settings) -> bool:
    """Whether a verification/reset link may be returned in the response.

    Never in production. This deployment has no mail transport, so the links
    have to reach a developer somehow; returning them to the caller is
    acceptable on a laptop and is credential disclosure anywhere else.
    """
    return not settings.is_production


def _set_session_cookie(
    response: Response, settings: Settings, token: str, *, max_age: int
) -> None:
    """Attach the session cookie with every flag that matters.

    - `httponly` keeps it out of `document.cookie`, so a script injected into
      the frontend cannot read it.
    - `secure` keeps it off plaintext connections.
    - `samesite` is the primary CSRF defence; `lax` stops the cookie riding
      along on a cross-site POST while still surviving ordinary navigation.
    - `path="/"` so logout can reliably clear the same cookie it set.
    """
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        path="/",
    )


def _clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
    )


def _reject_in_demo_mode(settings: Settings) -> None:
    """Refuse credential operations when the deployment has no real auth.

    In demo mode identity comes from a header, so a login endpoint would issue
    a session nothing consults — an invitation to believe the deployment is
    authenticated when it is not.
    """
    if settings.auth_mode is AuthMode.DEMO_HEADER:
        raise AuthorizationError(
            "This deployment runs in demo identity mode; the authentication "
            "endpoints are disabled. Set AUTH_MODE=session to enable them."
        )


# --- request models ---------------------------------------------------------


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=MAX_EMAIL)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD)
    display_name: str = Field(min_length=1, max_length=MAX_NAME)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=MAX_EMAIL)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD)


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=512)


class MfaCodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=16)


class MfaDisableRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=MAX_PASSWORD)
    code: str | None = Field(default=None, max_length=16)


class PasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=MAX_EMAIL)


class PasswordResetCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=1, max_length=MAX_PASSWORD)


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD)
    new_password: str = Field(min_length=1, max_length=MAX_PASSWORD)


class MemberRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=64)
    role: str = Field(min_length=1, max_length=32)


class SelectOrgRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: str = Field(min_length=1, max_length=64)


# --- registration and verification ------------------------------------------


@auth_router.post("/register")
def register(
    request: Request,
    payload: RegisterRequest,
    conn: sqlite3.Connection = DbDep,
) -> dict:
    """Create an account and issue an email-verification link.

    The response is identical whether or not the address was already
    registered, so this endpoint cannot be used to test which addresses have
    accounts.
    """
    settings = _settings(request)
    _reject_in_demo_mode(settings)

    try:
        _user_id, token = auth_service.register_user(
            conn,
            email=payload.email,
            password=payload.password,
            display_name=payload.display_name,
            client_ip=client_address(request),
        )
    except auth_service.RegistrationError as exc:
        # A rejected *password* is safe to explain: it says nothing about who
        # is registered, and a user who cannot see why their password was
        # refused simply tries another one that fails the same way.
        raise InvalidRequestError(str(exc)) from exc

    body: dict = {
        "status": "registration_received",
        "message": (
            "If that address is available, an account was created and a "
            "verification link issued."
        ),
    }
    if token and _may_disclose_link(settings):
        body["verification_token"] = token
        body["note"] = (
            "This deployment has no mail transport, so the link is returned "
            "here. It is withheld when APP_ENV is production."
        )
    return body


@auth_router.post("/verify-email")
def verify_email(
    request: Request, payload: TokenRequest, conn: sqlite3.Connection = DbDep
) -> dict:
    _reject_in_demo_mode(_settings(request))
    if not auth_service.verify_email(conn, token=payload.token):
        raise InvalidRequestError("That verification link is invalid or has expired.")
    return {"status": "verified"}


# --- login ------------------------------------------------------------------


@auth_router.post("/login")
def login(
    request: Request,
    response: Response,
    payload: LoginRequest,
    conn: sqlite3.Connection = DbDep,
) -> dict:
    """Verify a password and start a session.

    On success a cookie is set and the body says whether a second factor is
    still outstanding. When it is, the session exists but is marked
    unsatisfied, and `authenticate` will admit it to `/mfa/challenge` and
    nowhere else.
    """
    settings = _settings(request)
    _reject_in_demo_mode(settings)

    try:
        result = auth_service.login(
            conn,
            email=payload.email,
            password=payload.password,
            client_ip=client_address(request),
            user_agent=request.headers.get("user-agent"),
            require_verified_email=settings.require_verified_email,
        )
    except auth_service.AccountLocked as exc:
        raise AuthenticationError(str(exc)) from exc
    except auth_service.AuthError as exc:
        raise AuthenticationError(str(exc)) from exc

    _set_session_cookie(
        response,
        settings,
        result.session_token,
        max_age=repo.ABSOLUTE_TIMEOUT_HOURS * 3600,
    )
    return {
        "status": "mfa_required" if result.mfa_pending else "authenticated",
        "mfa_required": result.mfa_pending,
        "user_id": result.user_id,
        "org_id": result.org_id,
    }


@auth_router.post("/mfa/challenge")
def mfa_challenge(
    request: Request, payload: MfaCodeRequest, conn: sqlite3.Connection = DbDep
) -> dict:
    """Complete the second factor on a half-authenticated session."""
    settings = _settings(request)
    _reject_in_demo_mode(settings)

    caller = authenticate(request, conn, settings, allow_pending_mfa=True)
    if caller.mfa_satisfied:
        return {"status": "authenticated"}

    session = repo.lookup_session(conn, request.cookies.get(settings.session_cookie_name, ""))
    if session is None:
        raise AuthenticationError("Your session has expired. Sign in again.")

    if not auth_service.complete_mfa(
        conn, session=session, code=payload.code, client_ip=client_address(request)
    ):
        raise AuthenticationError("That code is not valid.")
    return {"status": "authenticated"}


@auth_router.post("/logout")
def logout(
    request: Request, response: Response, conn: sqlite3.Connection = DbDep
) -> dict:
    """End this session. Idempotent, and never an error.

    A logout that fails leaves a user believing they signed out when they did
    not, so an unauthenticated call simply clears the cookie and reports
    success.
    """
    settings = _settings(request)
    _clear_session_cookie(response, settings)

    token = request.cookies.get(settings.session_cookie_name)
    if token:
        session = repo.lookup_session(conn, token)
        if session is not None:
            auth_service.logout(conn, session=session)
    return {"status": "signed_out"}


@auth_router.post("/logout-all")
def logout_all(
    request: Request, response: Response, conn: sqlite3.Connection = DbDep
) -> dict:
    """Revoke every session this user holds, on every device."""
    settings = _settings(request)
    caller = authenticate(request, conn, settings)
    revoked = repo.revoke_all_sessions(conn, caller.user_id)
    record_event(
        conn,
        AuditEvent.SESSION_REVOKED,
        actor_user_id=caller.user_id,
        org_id=caller.org_id,
        details={"sessions_revoked": revoked, "scope": "all"},
    )
    _clear_session_cookie(response, settings)
    return {"status": "signed_out", "sessions_revoked": revoked}


# --- the current caller -----------------------------------------------------


@auth_router.get("/me")
def me(request: Request, conn: sqlite3.Connection = DbDep) -> dict:
    """Who the caller is, and what their role permits.

    The frontend renders from this. It is *not* an authorization decision:
    every permission listed is re-checked server-side at the point of use, and
    a client that lies to itself about this response gains nothing.
    """
    settings = _settings(request)
    caller = authenticate(request, conn, settings)
    memberships = (
        []
        if caller.is_demo
        else [
            {"org_id": m.org_id, "org_name": m.org_name, "role": m.role.value}
            for m in repo.list_memberships(conn, caller.user_id)
        ]
    )
    return {
        "user_id": caller.user_id,
        "display_name": caller.display_name,
        "org_id": caller.org_id,
        "org_name": caller.org_name,
        "role": caller.org_role or caller.role.value,
        "permissions": sorted(p.value for p in caller.permissions),
        "account_scope": sorted(caller.allowed_account_ids or []),
        "memberships": memberships,
        "auth_mode": settings.auth_mode.value,
    }


@auth_router.post("/select-organization")
def select_organization(
    request: Request, payload: SelectOrgRequest, conn: sqlite3.Connection = DbDep
) -> dict:
    """Switch which organisation this session acts in.

    Membership is re-checked here, and the choice is written to the *session
    row* rather than trusted per-request. A non-member gets a 404 — the same
    answer an organisation that does not exist gets, so this endpoint cannot be
    used to discover which organisation ids are real.
    """
    settings = _settings(request)
    caller = authenticate(request, conn, settings)
    if caller.auth_session_id is None:
        raise AuthorizationError("Organisation switching requires a real session.")

    membership = repo.get_membership(
        conn, org_id=payload.org_id, user_id=caller.user_id
    )
    if membership is None:
        raise NotFoundError("No such organisation within your memberships.")

    repo.set_session_org(conn, caller.auth_session_id, membership.org_id)
    return {
        "status": "switched",
        "org_id": membership.org_id,
        "org_name": membership.org_name,
        "role": membership.role.value,
    }


# --- MFA enrolment ----------------------------------------------------------


@auth_router.post("/mfa/enrol")
def mfa_enrol(request: Request, conn: sqlite3.Connection = DbDep) -> dict:
    """Generate a TOTP secret. MFA is not enabled until a code is confirmed."""
    settings = _settings(request)
    _reject_in_demo_mode(settings)
    caller = authenticate(request, conn, settings)
    secret, uri = auth_service.begin_mfa_enrolment(conn, user_id=caller.user_id)
    return {
        "secret": secret,
        "otpauth_uri": uri,
        "status": "pending_confirmation",
        "message": (
            "Add this to an authenticator app, then confirm a code at "
            "/api/auth/mfa/confirm. Two-factor authentication is not active "
            "until you do."
        ),
    }


@auth_router.post("/mfa/confirm")
def mfa_confirm(
    request: Request, payload: MfaCodeRequest, conn: sqlite3.Connection = DbDep
) -> dict:
    settings = _settings(request)
    _reject_in_demo_mode(settings)
    caller = authenticate(request, conn, settings)
    if not auth_service.confirm_mfa_enrolment(
        conn, user_id=caller.user_id, code=payload.code
    ):
        raise InvalidRequestError("That code is not valid. Enrolment was not completed.")
    return {"status": "mfa_enabled"}


@auth_router.post("/mfa/disable")
def mfa_disable(
    request: Request, payload: MfaDisableRequest, conn: sqlite3.Connection = DbDep
) -> dict:
    settings = _settings(request)
    _reject_in_demo_mode(settings)
    caller = authenticate(request, conn, settings)
    if not auth_service.disable_mfa(
        conn, user_id=caller.user_id, password=payload.password
    ):
        raise AuthenticationError("Re-authentication failed; MFA is unchanged.")
    return {"status": "mfa_disabled"}


# --- password reset and change ----------------------------------------------


@auth_router.post("/password/reset-request")
def password_reset_request(
    request: Request, payload: PasswordResetRequest, conn: sqlite3.Connection = DbDep
) -> dict:
    """Ask for a reset link. Always answers the same way."""
    settings = _settings(request)
    _reject_in_demo_mode(settings)

    token = auth_service.request_password_reset(
        conn, email=payload.email, client_ip=client_address(request)
    )
    body: dict = {"status": "accepted", "message": auth_service.RESET_ACKNOWLEDGEMENT}
    if token and _may_disclose_link(settings):
        body["reset_token"] = token
    return body


@auth_router.post("/password/reset")
def password_reset(
    request: Request,
    response: Response,
    payload: PasswordResetCompleteRequest,
    conn: sqlite3.Connection = DbDep,
) -> dict:
    """Redeem a reset link. Every session the user holds is revoked."""
    settings = _settings(request)
    _reject_in_demo_mode(settings)
    try:
        ok = auth_service.complete_password_reset(
            conn, token=payload.token, new_password=payload.new_password
        )
    except auth_service.RegistrationError as exc:
        raise InvalidRequestError(str(exc)) from exc
    if not ok:
        raise InvalidRequestError("That reset link is invalid, used, or has expired.")
    _clear_session_cookie(response, settings)
    return {"status": "password_reset", "message": "All sessions were signed out."}


@auth_router.post("/password/change")
def password_change(
    request: Request, payload: PasswordChangeRequest, conn: sqlite3.Connection = DbDep
) -> dict:
    settings = _settings(request)
    _reject_in_demo_mode(settings)
    caller = authenticate(request, conn, settings)
    try:
        ok = auth_service.change_password(
            conn,
            user_id=caller.user_id,
            current_password=payload.current_password,
            new_password=payload.new_password,
            keep_session_id=caller.auth_session_id,
        )
    except auth_service.RegistrationError as exc:
        raise InvalidRequestError(str(exc)) from exc
    if not ok:
        raise AuthenticationError("Your current password was not correct.")
    return {"status": "password_changed", "message": "Other sessions were signed out."}


@auth_router.get("/sessions")
def list_sessions(request: Request, conn: sqlite3.Connection = DbDep) -> dict:
    """This user's sessions, so they can see a device they do not recognise."""
    settings = _settings(request)
    caller = authenticate(request, conn, settings)
    return {"sessions": repo.list_user_sessions(conn, caller.user_id)}


# --- membership administration ----------------------------------------------


@auth_router.get("/organization/members")
def organization_members(request: Request, conn: sqlite3.Connection = DbDep) -> dict:
    settings = _settings(request)
    caller = authenticate(request, conn, settings)
    if caller.org_id is None:
        raise NotFoundError("You are not a member of any organisation.")
    caller.require(Permission.MANAGE_MEMBERS)
    return {"org_id": caller.org_id, "members": repo.list_org_members(conn, caller.org_id)}


@auth_router.post("/organization/members/role")
def set_member_role(
    request: Request, payload: MemberRoleRequest, conn: sqlite3.Connection = DbDep
) -> dict:
    """Change a member's role within the caller's own organisation.

    Note what is *not* taken from the request: the organisation. It comes from
    the caller's session, so there is no `org_id` field an attacker could point
    at somebody else's tenancy.
    """
    settings = _settings(request)
    caller = authenticate(request, conn, settings)
    if caller.org_id is None:
        raise NotFoundError("You are not a member of any organisation.")
    caller.require(Permission.MANAGE_MEMBERS)

    try:
        role = OrgRole(payload.role.strip().lower())
    except ValueError as exc:
        raise InvalidRequestError(
            f"role must be one of: {', '.join(r.value for r in OrgRole)}"
        ) from exc

    # Only an owner may mint another owner. Without this, an admin could
    # promote themselves past the ceiling their own role is meant to have.
    if role is OrgRole.OWNER and not caller.has(Permission.DELETE_ORGANIZATION):
        raise AuthorizationError("Only an owner may grant the owner role.")

    target = repo.get_membership(
        conn, org_id=caller.org_id, user_id=payload.user_id.strip()
    )
    if target is None:
        raise NotFoundError("No such member in this organisation.")

    previous = target.role.value
    if not repo.set_member_role(
        conn, org_id=caller.org_id, user_id=payload.user_id.strip(), role=role
    ):
        raise NotFoundError("No such member in this organisation.")

    record_event(
        conn,
        AuditEvent.MEMBERSHIP_ROLE_CHANGED,
        actor_user_id=caller.user_id,
        actor_role=caller.org_role,
        org_id=caller.org_id,
        target_type="user",
        target_id=payload.user_id.strip(),
        details={"from": previous, "to": role.value},
    )
    return {"status": "role_updated", "user_id": payload.user_id, "role": role.value}


# --- audit ------------------------------------------------------------------


@auth_router.get("/audit")
def audit(request: Request, conn: sqlite3.Connection = DbDep, limit: int = 100) -> dict:
    """The security audit trail for the caller's own organisation.

    Scoped to `caller.org_id`, which comes from the session — there is no
    parameter that widens it. The chain-verification result travels with the
    entries so a reader can tell whether what they are looking at is intact.
    """
    settings = _settings(request)
    caller = authenticate(request, conn, settings)
    if caller.org_id is None:
        raise NotFoundError("You are not a member of any organisation.")
    caller.require(Permission.READ_AUDIT_LOG)

    intact, first_bad = verify_audit_chain(conn)
    return {
        "org_id": caller.org_id,
        "chain_intact": intact,
        "first_invalid_seq": first_bad,
        "events": list_events(conn, org_id=caller.org_id, limit=limit),
    }
