"""Sign-in with Google and GitHub (OAuth 2.0 authorization code + PKCE).

This is not a second authentication system. A provider sign-in ends in the
same place a password sign-in does — a row in `sessions`, an HttpOnly cookie,
the same `mfa_satisfied` rule, the same audit event — and the account it signs
in to is an ordinary row in `users`. What this module adds is the *proof*: a
provider vouching for an identity, and the rules for which ASTRION account
that identity may open.

**The flow.** `begin` mints a `state` and a PKCE verifier, stores the state's
digest with the verifier, and returns the provider URL; the route also sets the
state in an HttpOnly cookie. The provider redirects back to the callback, which
requires the query `state` to equal the cookie *and* to name an unexpired,
unused row (`consume_state`). Only then is the code exchanged — with the
verifier — for an access token, which is used once to read the profile and
then dropped. Nothing the provider returns is stored except the stable subject
id and the address at link time.

**Which account an identity opens** (`resolve`):

1. An identity already linked to a user opens that user.
2. Otherwise, if the provider says the address is *verified*:
   - an existing account with that address is linked and opened. If that
     account had never verified its address, whoever set its password never
     proved they owned the mailbox — so the password is discarded (the owner
     can set one with a reset link), open sessions and outstanding codes are
     revoked, and the address is marked verified. This closes the
     "pre-registration" takeover, where an attacker registers a victim's
     address first and waits for the victim to arrive by Google.
   - otherwise a new, verified account is created, with no password.
3. Otherwise — GitHub with no verified address, or a provider that marks the
   address unverified — no account is linked or created from it. The person
   is asked for an address and proves it with an emailed code; step 2 then
   runs against the address they proved.

An identity is never linked on the strength of an unverified address.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from app.backend.db.errors import IntegrityError
from app.backend.auth import repository as repo
from app.backend.auth.passwords import UNUSABLE_PASSWORD
from app.backend.auth.tokens import hash_token, new_id, new_token, tokens_equal
from app.backend.services.audit import AuditEvent, hash_identifier, record_event

logger = logging.getLogger("astrion.auth.oauth")

PROVIDERS = ("google", "github")
STATE_TTL_MINUTES = 10
HTTP_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class ProviderEndpoints:
    authorize_url: str
    token_url: str
    profile_url: str
    emails_url: str | None = None
    scope: str = ""


#: The real endpoints. Not configurable from the environment: a deployment has
#: no reason to send its users' authorization codes anywhere else. Tests swap
#: them through `app.state.oauth_endpoints`.
DEFAULT_ENDPOINTS: dict[str, ProviderEndpoints] = {
    "google": ProviderEndpoints(
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        profile_url="https://openidconnect.googleapis.com/v1/userinfo",
        scope="openid email profile",
    ),
    "github": ProviderEndpoints(
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        profile_url="https://api.github.com/user",
        emails_url="https://api.github.com/user/emails",
        scope="read:user user:email",
    ),
}

PROVIDER_LABELS = {"google": "Google", "github": "GitHub"}


class OAuthError(Exception):
    """A provider sign-in could not complete. `code` is safe to show a browser."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


@dataclass(frozen=True)
class ProviderProfile:
    provider: str
    subject: str
    email: str | None
    email_verified: bool
    display_name: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


_PROVIDER_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _provider_error(payload) -> str:
    """The provider's own machine-readable reason for refusing, or "".

    GitHub answers a rejected token exchange with HTTP 200 and `{"error": ...}`
    (`redirect_uri_mismatch`, `incorrect_client_credentials`,
    `bad_verification_code`), and Google with a 4xx carrying the same field. Those
    are fixed lowercase codes, the difference between "the callback URL is not
    the one registered" and "the secret is wrong", so they are kept. Anything
    that does not look like such a code is dropped rather than trusted, and the
    free-text `error_description` is never read.
    """
    error = payload.get("error") if isinstance(payload, dict) else None
    return error if isinstance(error, str) and _PROVIDER_ERROR_CODE.match(error) else ""


def _client_credentials(settings, provider: str) -> tuple[str, str]:
    if provider not in settings.oauth_providers:
        raise OAuthError("oauth_unavailable")
    if provider == "google":
        return settings.google_oauth_client_id, settings.google_oauth_client_secret
    return settings.github_oauth_client_id, settings.github_oauth_client_secret


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# --- the authorization request ----------------------------------------------


def begin(
    conn: sqlite3.Connection,
    settings,
    provider: str,
    endpoints: ProviderEndpoints,
) -> tuple[str, str]:
    """Start a sign-in. Returns (state for the cookie, provider URL)."""
    client_id, _secret = _client_credentials(settings, provider)
    state = new_token()
    verifier = new_token()
    now = _now()
    with conn:
        # Housekeeping: abandoned flows are worthless after their TTL.
        conn.execute(
            "DELETE FROM oauth_states WHERE expires_at_utc < ?",
            ((now - timedelta(days=1)).isoformat(),),
        )
        conn.execute(
            """
            INSERT INTO oauth_states
                (state_hash, provider, code_verifier, created_at_utc, expires_at_utc)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                hash_token(state),
                provider,
                verifier,
                now.isoformat(),
                (now + timedelta(minutes=STATE_TTL_MINUTES)).isoformat(),
            ),
        )
    params = {
        "client_id": client_id,
        "redirect_uri": settings.oauth_callback_url(provider),
        "response_type": "code",
        "scope": endpoints.scope,
        "state": state,
        "code_challenge": _pkce_challenge(verifier),
        "code_challenge_method": "S256",
    }
    if provider == "google":
        # Always show the account chooser, so "sign in with a different Google
        # account" is possible after signing out.
        params["prompt"] = "select_account"
    else:
        params["allow_signup"] = "true"
    return state, f"{endpoints.authorize_url}?{urlencode(params)}"


def consume_state(
    conn: sqlite3.Connection,
    *,
    provider: str,
    query_state: str | None,
    cookie_state: str | None,
) -> str:
    """Validate and spend a callback's state. Returns the PKCE verifier.

    Both halves are required: the cookie proves this browser started the flow,
    the row proves this server issued it and it has not been used.
    """
    if not query_state or not cookie_state or not tokens_equal(query_state, cookie_state):
        raise OAuthError("oauth_state")
    digest = hash_token(query_state)
    row = conn.execute(
        "SELECT * FROM oauth_states WHERE state_hash = ?", (digest,)
    ).fetchone()
    if row is None or row["provider"] != provider or row["consumed_at_utc"]:
        raise OAuthError("oauth_state")
    expires_at = datetime.fromisoformat(row["expires_at_utc"])
    if _now() >= expires_at:
        raise OAuthError("oauth_state")
    with conn:
        cursor = conn.execute(
            """
            UPDATE oauth_states SET consumed_at_utc = ?
             WHERE state_hash = ? AND consumed_at_utc IS NULL
            """,
            (_now().isoformat(), digest),
        )
    if cursor.rowcount != 1:
        raise OAuthError("oauth_state")
    return row["code_verifier"]


# --- talking to the provider ------------------------------------------------


def fetch_profile(
    settings,
    provider: str,
    *,
    code: str,
    code_verifier: str,
    endpoints: ProviderEndpoints,
    http=None,
) -> ProviderProfile:
    """Exchange the code and read who the provider says this is.

    The access token lives in a local variable for the length of this function.
    It is never stored, returned, or logged; neither is the code.
    """
    import httpx

    client_id, client_secret = _client_credentials(settings, provider)
    owns_client = http is None
    client = http or httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
    try:
        token_response = client.post(
            endpoints.token_url,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "redirect_uri": settings.oauth_callback_url(provider),
            },
            headers={"Accept": "application/json"},
        )
        if token_response.status_code != 200:
            try:
                reason = _provider_error(token_response.json())
            except ValueError:
                reason = ""
            raise OAuthError(
                "oauth_failed",
                f"token endpoint {token_response.status_code}"
                + (f" ({reason})" if reason else ""),
            )
        payload = token_response.json()
        access_token = payload.get("access_token") if isinstance(payload, dict) else None
        if not access_token:
            # GitHub answers a bad code with 200 and {"error": ...}.
            reason = _provider_error(payload)
            raise OAuthError(
                "oauth_failed",
                "no access token in token response" + (f" ({reason})" if reason else ""),
            )

        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        profile_response = client.get(endpoints.profile_url, headers=headers)
        if profile_response.status_code != 200:
            raise OAuthError("oauth_failed", f"profile endpoint {profile_response.status_code}")
        profile = profile_response.json()

        if provider == "google":
            subject = str(profile.get("sub") or "")
            email = profile.get("email")
            verified = profile.get("email_verified") is True
            name = profile.get("name") or ""
        else:
            subject = str(profile.get("id") or "")
            name = profile.get("name") or profile.get("login") or ""
            email, verified = None, False
            if endpoints.emails_url:
                emails_response = client.get(endpoints.emails_url, headers=headers)
                if emails_response.status_code == 200:
                    email, verified = _github_primary_email(emails_response.json())
        del access_token
    except OAuthError:
        raise
    except Exception as exc:  # network, JSON, timeout: all one answer
        raise OAuthError("oauth_failed", type(exc).__name__) from None
    finally:
        if owns_client:
            client.close()

    if not subject:
        raise OAuthError("oauth_failed", "profile had no subject")
    email = repo.normalize_email(email) if isinstance(email, str) and "@" in email else None
    return ProviderProfile(
        provider=provider,
        subject=subject,
        email=email,
        email_verified=bool(email) and verified,
        display_name=str(name).strip()[:200],
    )


def _github_primary_email(entries) -> tuple[str | None, bool]:
    """The primary *verified* address, else any verified one, else nothing.

    `/user` carries only the public address, which may be absent, stale or
    unverified; `/user/emails` is the authority on what GitHub has confirmed.
    """
    if not isinstance(entries, list):
        return None, False
    verified = [
        e for e in entries
        if isinstance(e, dict) and e.get("verified") is True and isinstance(e.get("email"), str)
    ]
    # GitHub's private relay address cannot receive mail from us.
    verified = [e for e in verified if not e["email"].endswith("@users.noreply.github.com")]
    primary = next((e for e in verified if e.get("primary") is True), None)
    chosen = primary or (verified[0] if verified else None)
    return (chosen["email"], True) if chosen else (None, False)


# --- identities and accounts ------------------------------------------------


def find_identity_user(
    conn: sqlite3.Connection, provider: str, subject: str
) -> repo.User | None:
    row = conn.execute(
        "SELECT user_id FROM user_identities WHERE provider = ? AND provider_subject = ?",
        (provider, subject),
    ).fetchone()
    return None if row is None else repo.get_user(conn, row["user_id"])


def list_identities(conn: sqlite3.Connection, user_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT provider FROM user_identities WHERE user_id = ? ORDER BY provider",
        (user_id,),
    ).fetchall()
    return [row["provider"] for row in rows]


def _touch_identity(conn: sqlite3.Connection, provider: str, subject: str) -> None:
    with conn:
        conn.execute(
            """
            UPDATE user_identities SET last_used_at_utc = ?
             WHERE provider = ? AND provider_subject = ?
            """,
            (_now().isoformat(), provider, subject),
        )


def _link(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    provider: str,
    subject: str,
    email: str,
    request_id: str | None,
    ip_hash: str | None,
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO user_identities
                (identity_id, user_id, provider, provider_subject, email_at_link,
                 created_at_utc, last_used_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("IDN"),
                user_id,
                provider,
                subject,
                email,
                _now().isoformat(),
                _now().isoformat(),
            ),
        )
    record_event(
        conn,
        AuditEvent.IDENTITY_LINKED,
        actor_user_id=user_id,
        request_id=request_id,
        ip_hash=ip_hash,
        details={"provider": provider, "email_domain": email.rsplit("@", 1)[-1]},
    )


def _claim_unverified_account(conn: sqlite3.Connection, user: repo.User) -> None:
    """A provider proved this address; nobody had. Discard the unproven password.

    See the module docstring: whoever registered this account never showed they
    control the mailbox, so nothing they set may survive the real owner's
    arrival.
    """
    from app.backend.auth import verification

    repo.set_password_hash(conn, user.user_id, UNUSABLE_PASSWORD)
    repo.revoke_all_sessions(conn, user.user_id)
    repo.invalidate_tokens(conn, user_id=user.user_id, purpose="email_verification")
    verification.close_for_user(conn, user.user_id)
    repo.mark_email_verified(conn, user.user_id)


def _fallback_display_name(email: str, provided: str | None) -> str:
    name = (provided or "").strip()
    return name[:200] if name else email.split("@", 1)[0][:200] or "ASTRION user"


def account_for_verified_email(
    conn: sqlite3.Connection,
    *,
    provider: str,
    subject: str,
    email: str,
    display_name: str | None,
    request_id: str | None = None,
    client_ip: str | None = None,
) -> repo.User:
    """Link (or create) the account for an address that has been *proven*.

    Proven by the provider (`email_verified`) or by an emailed code — never on
    the provider's say-so for an unverified address.
    """
    ip_hash = hash_identifier(client_ip)
    normalized = repo.normalize_email(email)
    user = repo.get_user_by_email(conn, normalized)
    if user is not None:
        if not user.is_active:
            raise OAuthError("oauth_account")
        if not user.email_verified:
            _claim_unverified_account(conn, user)
    else:
        try:
            user = repo.create_user(
                conn,
                email=normalized,
                display_name=_fallback_display_name(normalized, display_name),
                password_hash=UNUSABLE_PASSWORD,
                email_verified=True,
            )
        except IntegrityError:
            # Registered by someone else between the lookup and the insert.
            user = repo.get_user_by_email(conn, normalized)
            if user is None:
                raise OAuthError("oauth_failed") from None
            if not user.email_verified:
                _claim_unverified_account(conn, user)
        else:
            record_event(
                conn,
                AuditEvent.REGISTERED,
                actor_user_id=user.user_id,
                request_id=request_id,
                ip_hash=ip_hash,
                details={"email_domain": normalized.rsplit("@", 1)[-1], "method": provider},
            )
            record_event(
                conn, AuditEvent.EMAIL_VERIFIED, actor_user_id=user.user_id,
                request_id=request_id, details={"method": provider},
            )

    try:
        _link(
            conn,
            user_id=user.user_id,
            provider=provider,
            subject=subject,
            email=normalized,
            request_id=request_id,
            ip_hash=ip_hash,
        )
    except IntegrityError:
        # This provider identity was linked concurrently. It must be to this
        # same user; if it is not, refuse rather than pick one.
        linked = find_identity_user(conn, provider, subject)
        if linked is None or linked.user_id != user.user_id:
            raise OAuthError("oauth_account") from None
    refreshed = repo.get_user(conn, user.user_id)
    assert refreshed is not None
    return refreshed


def resolve(
    conn: sqlite3.Connection,
    profile: ProviderProfile,
    *,
    request_id: str | None = None,
    client_ip: str | None = None,
) -> repo.User | None:
    """The account this identity opens, or None when an address must be proven."""
    linked = find_identity_user(conn, profile.provider, profile.subject)
    if linked is not None:
        if not linked.is_active:
            raise OAuthError("oauth_account")
        _touch_identity(conn, profile.provider, profile.subject)
        return linked

    if profile.email and profile.email_verified:
        return account_for_verified_email(
            conn,
            provider=profile.provider,
            subject=profile.subject,
            email=profile.email,
            display_name=profile.display_name,
            request_id=request_id,
            client_ip=client_ip,
        )
    return None
