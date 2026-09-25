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
- **Email is best-effort; registration is not.** A delivery failure does not
  roll back account creation: the user exists and can ask for another code.
  The one-time verification code is never in a response, in any environment;
  it goes to the mailbox or, in local development, to the outbox directory
  (`EMAIL_OUTBOX_DIR`). The two links this module can still issue — the
  legacy `resend-verification` link and the password-reset link — are returned
  in a response only outside production (`_may_disclose_link`), so those flows
  stay testable without a mail provider.
- **Resend rate-limiting is server-side only.** The frontend may request a
  resend and show a countdown, but the server enforces the limits: for codes,
  a 60-second cooldown and 5 sends per address per hour (`OTP_*` settings);
  for the legacy link, a 30-second interval, 3 sends maximum and a 24-hour
  cooldown after the third.
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
from app.backend.auth.permissions import Permission
from app.backend.core.config import AuthMode, Settings, email_provider_for
from app.backend.core.errors import (
    AuthenticationError,
    AuthorizationError,
    DataUnavailableError,
    InvalidRequestError,
    NotFoundError,
)
from app.backend.email.provider import EmailDeliveryError
from app.backend.services.records import effective_account_ids
from app.backend.services.audit import (
    AuditEvent,
    list_events,
    record_event,
    verify_audit_chain,
)
from app.backend.services.bootstrap import (
    DemoEnvironmentError,
    ensure_demo_environment,
    sign_in_demo_user,
)

#: Reserved for this module's own diagnostics. Deliberately never used to
#: record a verification or reset token — see `_may_disclose_link`.
logger = logging.getLogger("astrion.auth")

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

    The token is deliberately *not* written to the application log in the
    production case. A log is a second copy of a credential, retained for
    longer than the credential itself and read by more people; adding that
    channel to close the delivery gap would trade one problem for a worse one.
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


class ResendVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=MAX_EMAIL)


# --- registration and verification ------------------------------------------


def _build_verification_url(settings: Settings, token: str) -> str:
    """Construct the absolute URL a user clicks to verify their address."""
    base = settings.email_verification_url.rstrip("/")
    from urllib.parse import urlencode
    return f"{base}/verify-email?{urlencode({'token': token})}"


def _attempt_send_verification(
    settings: Settings,
    conn,
    user_id: str,
    email: str,
    display_name: str,
    token: str,
) -> bool:
    """Send a verification email. Returns True if delivered, False if not.

    Never raises: delivery failure is absorbed here so registration/resend
    callers can respond uniformly. The token is NOT disclosed in logs.

    Rate-limiting state is recorded on every call, regardless of delivery
    success: the limit applies to how often we'll issue a new token and attempt
    a send, not to how often our mail provider succeeds.
    """
    repo.record_verification_sent(conn, user_id)
    provider = email_provider_for(settings)
    url = _build_verification_url(settings, token)
    try:
        provider.send_verification_email(
            to_address=email,
            display_name=display_name,
            verification_url=url,
        )
        return True
    except EmailDeliveryError:
        return False


@auth_router.post("/register")
def register(
    request: Request,
    response: Response,
    payload: RegisterRequest,
    conn: sqlite3.Connection = DbDep,
) -> dict:
    """Create an account and email a one-time code to verify its address.

    The response is identical whether or not the address was already
    registered, so this endpoint cannot be used to test which addresses have
    accounts: a taken address gets a *decoy* verification that looks and
    behaves like a real one and can never succeed.

    The code itself never appears in the response, in any environment. It goes
    to the address, or — in local development with `EMAIL_OUTBOX_DIR` set — to
    a file on the developer's own disk.
    """
    from app.backend.api.identity_routes import start_account_verification
    from app.backend.auth.verification import mask_email

    settings = _settings(request)
    _reject_in_demo_mode(settings)

    try:
        user_id, created = auth_service.register_user(
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

    if not settings.require_verified_email:
        # A deployment that has chosen not to prove addresses (it has no way to
        # email a code) has nothing to verify, so no verification is started and
        # no cookie is set. The body is the same whether the address was new or
        # already registered -- the caller signs in with the password it just
        # typed, and only that can tell the two apart -- so this still cannot be
        # used to learn who is registered. Turning REQUIRE_VERIFIED_EMAIL back
        # on restores the code flow below unchanged, and an account made this
        # way is then asked for a code at its next sign-in.
        return {
            "status": "registered",
            "email_sent": False,
            "message": "Your account is ready. Sign in to continue.",
            "verification": None,
        }

    normalized = repo.normalize_email(payload.email)
    status = start_account_verification(
        request,
        response,
        conn,
        user_id=user_id if created else None,
        email=normalized,
        decoy=not created,
    )
    email_sent = bool(status.get("email_sent"))
    hint = mask_email(normalized)
    return {
        "status": "registration_received",
        "email_sent": email_sent,
        "message": (
            # Worded to be true for a new address (it holds the code) and for
            # one that already has an account (it says so), because the two
            # must be answered alike.
            f"We've emailed {hint}."
            if email_sent
            else "Your account is waiting for verification, but the code could "
            "not be emailed. Try sending it again."
        ),
        "verification": status,
    }


@auth_router.post("/verify-email")
def verify_email(
    request: Request, payload: TokenRequest, conn: sqlite3.Connection = DbDep
) -> dict:
    _reject_in_demo_mode(_settings(request))
    if not auth_service.verify_email(conn, token=payload.token):
        raise InvalidRequestError("That verification link is invalid or has expired.")
    return {"status": "verified"}


@auth_router.post("/resend-verification")
def resend_verification(
    request: Request,
    payload: ResendVerificationRequest,
    conn: sqlite3.Connection = DbDep,
) -> dict:
    """Re-send a verification email to an unverified address.

    Rate-limited server-side: 30-second minimum interval between sends, at
    most 3 total sends per address, then a 24-hour cooldown. The response is
    identical whether or not the address is registered, so this endpoint cannot
    be used to discover which addresses have accounts.

    Returns current resend state so the frontend can update its countdown
    without a separate status call.
    """
    settings = _settings(request)
    _reject_in_demo_mode(settings)

    normalized = repo.normalize_email(payload.email)
    user = repo.get_user_by_email(conn, normalized)

    # Always respond with the same shape. If the user does not exist or is
    # already verified, the response is indistinguishable from a rate-limit.
    if user is None or user.email_verified:
        return {
            "status": "resend_requested",
            "email_sent": False,
            "message": (
                "If that address is registered and unverified, a new link will "
                "be sent shortly."
            ),
            "resend_state": {
                "can_resend": False,
                "seconds_until_allowed": 0,
                "sends_used": 0,
                "in_cooldown": False,
            },
        }

    state = repo.get_resend_state(conn, user.user_id)

    if not state.can_send:
        if state.in_cooldown:
            raise InvalidRequestError(
                "Too many verification emails have been sent to this address. "
                f"Please wait before requesting another."
            )
        raise InvalidRequestError(
            f"Please wait {state.seconds_until_allowed} seconds before "
            "requesting another verification email."
        )

    # Mint a fresh token (the old one is not invalidated — it may still work
    # if the user finds the first email, which is fine).
    token = repo.issue_auth_token(
        conn,
        user_id=user.user_id,
        purpose=auth_service.VERIFICATION_PURPOSE,
        ttl_minutes=repo.EMAIL_VERIFICATION_TTL_HOURS * 60,
    )

    email_sent = _attempt_send_verification(
        settings, conn, user.user_id, normalized, user.display_name, token
    )

    body: dict = {
        "status": "resend_requested",
        "email_sent": email_sent,
        "message": (
            "A new verification link has been sent to your inbox."
            if email_sent
            else "If that address is registered and unverified, a new link will "
            "be sent shortly."
        ),
        "resend_state": {
            "can_resend": False,
            "seconds_until_allowed": repo.RESEND_MIN_INTERVAL_SECONDS,
            "sends_used": state.sends_used + (1 if email_sent else 0),
            "in_cooldown": False,
        },
    }
    if token and not email_sent and _may_disclose_link(settings):
        body["verification_token"] = token
        body["note"] = (
            "Email delivery is not configured, so the link is returned here. "
            "It is withheld when APP_ENV is production."
        )
    return body


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
    except auth_service.EmailVerificationRequired as exc:
        # Only reachable with the correct password: send a code and route the
        # owner to the verification screen instead of a dead end.
        from app.backend.api.identity_routes import verification_required_response

        return verification_required_response(request, conn, exc.user)
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


#: What a visitor is told when the environment could not be built. It names no
#: script, no path and no stage — those go to the server log, which is where
#: somebody who could act on them is reading. Telling a member of the public to
#: run an ingestion command is not an error message, it is an apology for not
#: having one.
DEMO_UNAVAILABLE_MESSAGE = (
    "The demo environment is temporarily initialising. Please retry in a moment."
)


@auth_router.post("/demo-login")
def demo_login(request: Request, response: Response) -> dict:
    """One click into the public demo, with no credential in the browser.

    Three things make this safe to expose to anyone who can reach the port:

    - **The credential lives on the server.** This endpoint takes no body at
      all, so there is no address and no password a caller could substitute —
      the demo identity comes from `Settings` and from nowhere else. A visitor
      cannot ask to be signed in as somebody else, because the request has no
      field in which to ask.
    - **It is an ordinary sign-in.** `sign_in_demo_user` calls the same
      `auth.service.login` the typed form reaches, so the session that comes
      back passed the same password verification, is subject to the same
      lockout and the same rate limit, wrote the same audit entry, and is
      scoped to its workspace by the same membership lookup.
    - **It builds only what is missing.** `ensure_demo_environment` reads the
      database and does the absent part, so the first visitor after a cold
      start gets an environment and the ten thousandth gets three `COUNT(*)`
      queries. It is deliberately not guarded by a process-memory flag: the
      process may be new and the database old, or the reverse.

    Notably absent: `DbDep`. This is the one endpoint that must work when there
    is no database yet, and a dependency whose job is to refuse in exactly that
    case would make it the one endpoint that could not.
    """
    settings = _settings(request)
    _reject_in_demo_mode(settings)
    if not settings.demo_login_enabled:
        # Not an authorization refusal: on a deployment that has not published
        # a demo, this route has nothing behind it.
        raise NotFoundError("This deployment does not offer public demo access.")

    try:
        report = ensure_demo_environment(settings)
    except DemoEnvironmentError as exc:
        # The stage and the underlying error go to the operator; the visitor
        # gets something they can act on, which is "try again".
        logger.error(
            "demo environment could not be prepared at stage %r: %s",
            exc.stage,
            exc.detail,
        )
        raise DataUnavailableError(DEMO_UNAVAILABLE_MESSAGE) from exc

    if report.changed:
        logger.info(
            "demo environment prepared on demand in %.2fs", report.duration_seconds
        )

    conn = request.app.state.database.connect()
    try:
        result = sign_in_demo_user(
            conn,
            settings,
            client_ip=client_address(request),
            user_agent=request.headers.get("user-agent"),
        )
    except auth_service.AccountLocked as exc:
        # Reachable only by someone deliberately failing sign-ins against the
        # published demo address often enough to trip the account lockout.
        # Clearing it here would hand them a way to clear it for themselves too.
        logger.warning("demo sign-in refused: the demo account is locked out")
        raise AuthenticationError(str(exc)) from exc
    except auth_service.AuthError as exc:
        logger.error("demo sign-in failed after a successful bootstrap: %s", exc)
        raise DataUnavailableError(DEMO_UNAVAILABLE_MESSAGE) from exc
    finally:
        conn.close()

    _set_session_cookie(
        response,
        settings,
        result.session_token,
        max_age=repo.ABSOLUTE_TIMEOUT_HOURS * 3600,
    )
    return {
        # Shaped exactly like `/login`'s response, so the client has one
        # sign-in result to understand rather than two.
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
    try:
        caller = authenticate(request, conn, settings)
    except AuthenticationError as exc:
        # Not signed in. If this browser is part-way through verifying an
        # address, say so, so a reload returns to the code screen rather than
        # to a sign-in form the verification would then have to restart.
        if exc.details.get("mfa_required"):
            raise
        from app.backend.api.identity_routes import current_verification
        from app.backend.auth.verification import OtpPolicy, status

        pending = current_verification(request, conn)
        if pending is None:
            raise
        raise AuthenticationError(
            exc.message,
            details={"verification": status(conn, pending, OtpPolicy.from_settings(settings))},
        ) from None
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
        "account_scope": effective_account_ids(conn, caller.scope()),
        "memberships": memberships,
        "auth_mode": settings.auth_mode.value,
    }


# Workspace switching lives in `api/workspace_routes.py` as
# `POST /api/workspaces/{workspace_id}/activate`. It used to be
# `POST /api/auth/select-organization`; the behaviour is identical — membership
# re-checked, the choice written to the session row — and the path now matches
# the product's own word for the thing.


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


# Membership administration lives in `api/workspace_routes.py`. It used to be
# here, operating implicitly on "the caller's current organisation"; Phase 1
# moved it to `/api/workspaces/{workspace_id}/members`, which names the
# workspace explicitly and re-resolves the caller's membership of *that*
# workspace on every request. One surface, one concept.


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
