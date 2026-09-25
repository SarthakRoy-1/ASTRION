"""Email verification codes and sign-in with Google / GitHub.

Two cookies are introduced, both `HttpOnly` and both carrying only a random
value whose digest is stored server-side — the same shape as the session:

- `<session cookie>_verification` binds a browser to one email verification
  (`auth/verification.py`). The verify, resend and address endpoints take no
  email address; they act on the verification this cookie names, which exists
  only for someone who created the account or just gave its correct password.
- `<session cookie>_oauth_state` binds a browser to one provider sign-in in
  flight. The callback refuses unless the query `state` equals it.

Neither the one-time code nor a provider's authorization code or access token
ever appears in a response body, a redirect URL, or a log line.

Every browser-facing redirect goes to `FRONTEND_BASE_URL/sign-in`, with at most
a fixed error code appended. No request parameter chooses the destination, so
there is no open redirect to abuse.
"""

from __future__ import annotations

import logging
import sqlite3
from http import HTTPStatus
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from app.backend.api.auth_routes import (
    MAX_EMAIL,
    _reject_in_demo_mode,
    _set_session_cookie,
    _settings,
)
from app.backend.api.dependencies import DbDep
from app.backend.api.errors import error_response
from app.backend.api.ratelimit import client_address
from app.backend.auth import oauth
from app.backend.auth import repository as repo
from app.backend.auth import service as auth_service
from app.backend.auth import verification as ver
from app.backend.core.config import AuthMode, Settings, email_provider_for
from app.backend.core.errors import AppError, InvalidRequestError, NotFoundError
from app.backend.email.provider import EmailDeliveryError, NullEmailProvider
from app.backend.services.audit import (
    AuditEvent,
    AuditOutcome,
    hash_identifier,
    record_event,
)

logger = logging.getLogger("astrion.auth")

identity_router = APIRouter(prefix="/api/auth", tags=["auth"])


class VerificationError(AppError):
    """A code or verification was refused. `code` says which way, for the UI."""

    status = HTTPStatus.BAD_REQUEST

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: HTTPStatus = HTTPStatus.BAD_REQUEST,
        details: dict | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.code = code
        self.status = status


VERIFICATION_EXPIRED = (
    "This verification has expired. Sign in again to get a new code."
)


# --- cookies ------------------------------------------------------------------


def verification_cookie_name(settings: Settings) -> str:
    return f"{settings.session_cookie_name}_verification"


def oauth_state_cookie_name(settings: Settings) -> str:
    return f"{settings.session_cookie_name}_oauth_state"


def _set_verification_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        key=verification_cookie_name(settings),
        value=token,
        max_age=settings.verification_ttl_minutes * 60,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        path="/api/auth",
    )


def clear_verification_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=verification_cookie_name(settings),
        path="/api/auth",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
    )


def current_verification(
    request: Request, conn: sqlite3.Connection
) -> ver.Verification | None:
    settings = _settings(request)
    return ver.find_verification(
        conn, request.cookies.get(verification_cookie_name(settings))
    )


def _policy(request: Request) -> ver.OtpPolicy:
    return ver.OtpPolicy.from_settings(_settings(request))


# --- sending a code -----------------------------------------------------------


def _email_provider(request: Request):
    """The configured transport. Tests install a capturing one on app.state."""
    override = getattr(request.app.state, "email_provider", None)
    return override if override is not None else email_provider_for(_settings(request))


def _display_name_for(conn: sqlite3.Connection, verification: ver.Verification) -> str:
    if verification.user_id:
        user = repo.get_user(conn, verification.user_id)
        if user is not None:
            return user.display_name
    name = (verification.provider_display_name or "").strip()
    if name:
        return name
    return (verification.email or "there").split("@", 1)[0]


def send_code(
    request: Request, conn: sqlite3.Connection, verification: ver.Verification
) -> bool:
    """Issue a fresh code and email it. Returns whether it was delivered.

    Raises `SendRefused` when a code was asked for too soon or too often. A
    decoy's code is minted and counted but never sent, and it reports the
    delivery the deployment *would* have managed, so it reads exactly like a
    real one.
    """
    policy = _policy(request)
    issued = ver.issue_code(conn, verification, policy)

    if verification.decoy:
        # What a real send through this transport would report. Only a live
        # provider outage could make the two differ.
        return not isinstance(_email_provider(request), NullEmailProvider)

    domain = (verification.email or "").rsplit("@", 1)[-1]
    try:
        _email_provider(request).send_verification_code(
            to_address=verification.email,
            display_name=_display_name_for(conn, verification),
            code=issued.code,
            expires_minutes=policy.ttl_minutes,
        )
    except EmailDeliveryError:
        record_event(
            conn,
            AuditEvent.EMAIL_CODE_FAILED,
            outcome=AuditOutcome.FAILURE,
            actor_user_id=verification.user_id,
            ip_hash=hash_identifier(client_address(request)),
            details={"purpose": verification.purpose, "email_domain": domain},
        )
        return False
    ver.mark_delivered(conn, issued.otp_id)
    record_event(
        conn,
        AuditEvent.EMAIL_CODE_SENT,
        actor_user_id=verification.user_id,
        ip_hash=hash_identifier(client_address(request)),
        details={"purpose": verification.purpose, "email_domain": domain},
    )
    return True


def _try_send(
    request: Request, conn: sqlite3.Connection, verification: ver.Verification
) -> bool:
    """`send_code`, where being refused just means "not sent this time"."""
    try:
        return send_code(request, conn, verification)
    except ver.SendRefused:
        return False


def start_account_verification(
    request: Request,
    response: Response,
    conn: sqlite3.Connection,
    *,
    user_id: str | None,
    email: str,
    decoy: bool = False,
) -> dict:
    """Open a verification for an account, send its first code, set the cookie.

    A browser that already holds a live verification for the same account
    keeps it — signing in twice in one minute is not a reason to mint a second
    verification — and is only sent a new code when it has no live one.
    """
    settings = _settings(request)
    policy = _policy(request)

    existing = current_verification(request, conn)
    if (
        existing is not None
        and not decoy
        and existing.purpose == ver.PURPOSE_ACCOUNT
        and existing.user_id == user_id
    ):
        state = ver.status(conn, existing, policy)
        if state["code_expires_in_seconds"]:
            # The code already on its way still works; say nothing new.
            return state
        sent = _try_send(request, conn, existing)
        return ver.status(conn, existing, policy, email_sent=sent)

    token, verification = ver.open_verification(
        conn,
        purpose=ver.PURPOSE_ACCOUNT,
        policy=policy,
        user_id=user_id,
        email=email,
        decoy=decoy,
    )
    sent = _try_send(request, conn, verification)
    _set_verification_cookie(response, settings, token)
    return ver.status(conn, verification, policy, email_sent=sent)


def verification_required_response(
    request: Request, conn: sqlite3.Connection, user: repo.User
) -> JSONResponse:
    """The answer to a correct password on an address that was never proven."""
    carrier = Response()
    status = start_account_verification(
        request, carrier, conn, user_id=user.user_id, email=user.email
    )
    body = error_response(
        int(HTTPStatus.UNAUTHORIZED),
        auth_service.EmailVerificationRequired.code,
        "Verify your email address to finish signing in.",
        details={"verification": status},
        request_id=getattr(request.state, "request_id", None),
    )
    for name, value in carrier.headers.items():
        if name.lower() == "set-cookie":
            body.headers.append("set-cookie", value)
    return body


# --- the verification endpoints -------------------------------------------------


class CodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=16)


class AddressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=MAX_EMAIL)


def _require_verification(request: Request, conn: sqlite3.Connection) -> ver.Verification:
    verification = current_verification(request, conn)
    if verification is None:
        raise VerificationError("verification_expired", VERIFICATION_EXPIRED)
    return verification


@identity_router.get("/verification")
def verification_status(request: Request, conn: sqlite3.Connection = DbDep) -> dict:
    """The verification this browser is in the middle of, if any."""
    _reject_in_demo_mode(_settings(request))
    verification = current_verification(request, conn)
    if verification is None:
        return {"pending": False, "verification": None}
    return {"pending": True, "verification": ver.status(conn, verification, _policy(request))}


@identity_router.post("/verification/email")
def verification_address(
    request: Request, payload: AddressRequest, conn: sqlite3.Connection = DbDep
) -> dict:
    """Choose the address a Google/GitHub sign-in will prove, and send it a code.

    Only for a provider sign-in that arrived without a verified address. The
    answer is the same whether or not the address belongs to an account:
    which one it becomes is decided only once the code proves the mailbox.
    """
    _reject_in_demo_mode(_settings(request))
    verification = _require_verification(request, conn)
    if verification.purpose != ver.PURPOSE_OAUTH:
        raise InvalidRequestError("This verification's address is already fixed.")
    email = repo.normalize_email(payload.email)
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise InvalidRequestError("Enter a valid email address.")

    if verification.email != email:
        verification = ver.set_email(conn, verification, email)
    sent = _send_or_refuse(request, conn, verification)
    return {"status": "code_sent" if sent else "delivery_failed",
            "verification": ver.status(conn, verification, _policy(request), email_sent=sent)}


def _send_or_refuse(
    request: Request, conn: sqlite3.Connection, verification: ver.Verification
) -> bool:
    try:
        return send_code(request, conn, verification)
    except ver.SendRefused as refused:
        if refused.reason == "cooldown":
            raise VerificationError(
                "otp_resend_cooldown",
                f"Please wait {refused.retry_after_seconds} seconds before "
                "requesting another code.",
                status=HTTPStatus.TOO_MANY_REQUESTS,
                details={"retry_after_seconds": refused.retry_after_seconds},
            ) from None
        raise VerificationError(
            "otp_send_limit",
            "Too many codes have been requested for this address. Try again later.",
            status=HTTPStatus.TOO_MANY_REQUESTS,
            details={"retry_after_seconds": refused.retry_after_seconds},
        ) from None


@identity_router.post("/verification/resend")
def verification_resend(request: Request, conn: sqlite3.Connection = DbDep) -> dict:
    """Send a fresh code. The previous one stops working immediately."""
    _reject_in_demo_mode(_settings(request))
    verification = _require_verification(request, conn)
    if verification.email is None:
        raise InvalidRequestError("Enter an email address first.")
    sent = _send_or_refuse(request, conn, verification)
    return {"status": "code_sent" if sent else "delivery_failed",
            "verification": ver.status(conn, verification, _policy(request), email_sent=sent)}


@identity_router.post("/verification/verify")
def verification_verify(
    request: Request,
    response: Response,
    payload: CodeRequest,
    conn: sqlite3.Connection = DbDep,
) -> dict:
    """Check a code. On success the address is verified and a session begins.

    The session is the same kind a password sign-in issues, and it honours the
    account's second factor: with MFA on, it is a half session and the answer
    says `mfa_required`.
    """
    settings = _settings(request)
    _reject_in_demo_mode(settings)
    verification = _require_verification(request, conn)
    if verification.email is None:
        raise InvalidRequestError("Enter an email address first.")

    policy = _policy(request)
    outcome = ver.verify_code(conn, verification, payload.code, policy)
    if outcome.result is ver.VerifyResult.INVALID:
        raise VerificationError(
            "otp_invalid",
            "That code is incorrect.",
            details={"attempts_remaining": outcome.attempts_remaining},
        )
    if outcome.result is ver.VerifyResult.EXPIRED:
        raise VerificationError(
            "otp_expired", "That code has expired. Request a new one."
        )
    if outcome.result is ver.VerifyResult.ATTEMPTS_EXCEEDED:
        raise VerificationError(
            "otp_attempts_exceeded",
            "Too many incorrect attempts. Request a new code.",
        )

    if not ver.complete(conn, verification):
        raise VerificationError("verification_expired", VERIFICATION_EXPIRED)

    ip = client_address(request)
    user_agent = request.headers.get("user-agent")
    if verification.purpose == ver.PURPOSE_ACCOUNT:
        user = repo.get_user(conn, verification.user_id or "")
        if user is None or not user.is_active:
            raise VerificationError("verification_expired", VERIFICATION_EXPIRED)
        repo.mark_email_verified(conn, user.user_id)
        repo.invalidate_tokens(
            conn, user_id=user.user_id, purpose=auth_service.VERIFICATION_PURPOSE
        )
        ver.close_for_user(conn, user.user_id)
        record_event(
            conn,
            AuditEvent.EMAIL_VERIFIED,
            actor_user_id=user.user_id,
            ip_hash=hash_identifier(ip),
            details={"method": "email_code"},
        )
        user = repo.get_user(conn, user.user_id)
        assert user is not None
        method = "email_code"
    else:
        try:
            user = oauth.account_for_verified_email(
                conn,
                provider=verification.provider or "",
                subject=verification.provider_subject or "",
                email=verification.email,
                display_name=verification.provider_display_name,
                client_ip=ip,
            )
        except oauth.OAuthError as exc:
            raise VerificationError(
                exc.code, "This sign-in could not be completed. Try again."
            ) from None
        method = verification.provider or "oauth"

    result = auth_service.start_session(
        conn, user=user, method=method, client_ip=ip, user_agent=user_agent
    )
    _set_session_cookie(
        response, settings, result.session_token,
        max_age=repo.ABSOLUTE_TIMEOUT_HOURS * 3600,
    )
    clear_verification_cookie(response, settings)
    return {
        "status": "mfa_required" if result.mfa_pending else "authenticated",
        "mfa_required": result.mfa_pending,
        "user_id": result.user_id,
        "org_id": result.org_id,
    }


@identity_router.post("/verification/cancel")
def verification_cancel(
    request: Request, response: Response, conn: sqlite3.Connection = DbDep
) -> dict:
    """Abandon the verification in this browser. Idempotent."""
    settings = _settings(request)
    verification = current_verification(request, conn)
    if verification is not None:
        ver.complete(conn, verification)
    clear_verification_cookie(response, settings)
    return {"status": "cancelled"}


# --- Google / GitHub ------------------------------------------------------------


#: The only values a callback ever appends to the frontend URL.
OAUTH_ERROR_CODES = frozenset(
    {"oauth_cancelled", "oauth_failed", "oauth_state", "oauth_unavailable", "oauth_account"}
)


def _frontend_redirect(settings: Settings, **params: str) -> RedirectResponse:
    query = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(
        f"{settings.frontend_base_url.rstrip('/')}/sign-in{query}", status_code=302
    )


def _oauth_failure(settings: Settings, code: str) -> RedirectResponse:
    return _frontend_redirect(
        settings, auth_error=code if code in OAUTH_ERROR_CODES else "oauth_failed"
    )


def _endpoints(request: Request, provider: str) -> oauth.ProviderEndpoints:
    overrides = getattr(request.app.state, "oauth_endpoints", None) or {}
    return overrides.get(provider) or oauth.DEFAULT_ENDPOINTS[provider]


def _known_provider(provider: str) -> str:
    if provider not in oauth.PROVIDERS:
        raise NotFoundError("No such sign-in provider.")
    return provider


@identity_router.get("/oauth/{provider}/start")
def oauth_start(
    provider: str, request: Request, conn: sqlite3.Connection = DbDep
) -> RedirectResponse:
    """Send the browser to the provider. Reached by navigation, not by fetch."""
    settings = _settings(request)
    _known_provider(provider)
    if settings.auth_mode is AuthMode.DEMO_HEADER or provider not in settings.oauth_providers:
        return _oauth_failure(settings, "oauth_unavailable")

    state, url = oauth.begin(conn, settings, provider, _endpoints(request, provider))
    response = RedirectResponse(url, status_code=302)
    # `Lax`, whatever the session cookie uses: the provider brings the browser
    # back with a top-level GET, which is exactly what Lax lets through, and a
    # cross-site POST is exactly what it does not.
    response.set_cookie(
        key=oauth_state_cookie_name(settings),
        value=state,
        max_age=oauth.STATE_TTL_MINUTES * 60,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/api/auth/oauth",
    )
    return response


@identity_router.get("/oauth/{provider}/callback")
def oauth_callback(
    provider: str,
    request: Request,
    conn: sqlite3.Connection = DbDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Finish a provider sign-in and return the browser to the frontend."""
    settings = _settings(request)
    _known_provider(provider)
    ip = client_address(request)

    def fail(reason: str, detail: str = "") -> RedirectResponse:
        details = {"provider": provider, "reason": reason}
        if detail:
            # Only ever an `OAuthError`'s detail: a status code, an exception
            # class or a provider's fixed error code, never a token, a code or
            # anything the visitor typed. It is what tells "wrong secret" from
            # "wrong callback URL" after the fact.
            details["detail"] = detail[:200]
        record_event(
            conn,
            AuditEvent.OAUTH_FAILED,
            outcome=AuditOutcome.FAILURE,
            ip_hash=hash_identifier(ip),
            details=details,
        )
        response = _oauth_failure(settings, reason)
        response.delete_cookie(oauth_state_cookie_name(settings), path="/api/auth/oauth")
        return response

    if settings.auth_mode is AuthMode.DEMO_HEADER or provider not in settings.oauth_providers:
        return fail("oauth_unavailable")

    cookie_state = request.cookies.get(oauth_state_cookie_name(settings))
    try:
        verifier = oauth.consume_state(
            conn, provider=provider, query_state=state, cookie_state=cookie_state
        )
    except oauth.OAuthError as exc:
        return fail(exc.code)

    if error:
        # `access_denied` is the person pressing Cancel at the provider.
        return fail("oauth_cancelled" if error == "access_denied" else "oauth_failed")
    if not code or len(code) > 2048:
        return fail("oauth_failed")

    try:
        profile = oauth.fetch_profile(
            settings,
            provider,
            code=code,
            code_verifier=verifier,
            endpoints=_endpoints(request, provider),
            http=getattr(request.app.state, "oauth_http", None),
        )
        user = oauth.resolve(conn, profile, client_ip=ip)
    except oauth.OAuthError as exc:
        # The detail names a status code or exception class, never a token.
        logger.warning("oauth %s sign-in failed: %s", provider, exc)
        return fail(exc.code, str(exc))

    if user is None:
        # No verified address from the provider: prove one by code first.
        token, _verification = ver.open_verification(
            conn,
            purpose=ver.PURPOSE_OAUTH,
            policy=_policy(request),
            provider=provider,
            provider_subject=profile.subject,
            provider_display_name=profile.display_name,
        )
        logger.info("oauth %s callback: no verified address; asking for one", provider)
        response = _frontend_redirect(settings, auth="verify_email")
        _set_verification_cookie(response, settings, token)
    else:
        result = auth_service.start_session(
            conn,
            user=user,
            method=provider,
            client_ip=ip,
            user_agent=request.headers.get("user-agent"),
        )
        logger.info("oauth %s callback: session started", provider)
        response = _frontend_redirect(settings)
        _set_session_cookie(
            response, settings, result.session_token,
            max_age=repo.ABSOLUTE_TIMEOUT_HOURS * 3600,
        )
        clear_verification_cookie(response, settings)

    response.delete_cookie(oauth_state_cookie_name(settings), path="/api/auth/oauth")
    return response
