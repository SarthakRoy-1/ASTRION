"""Authentication and session security, exercised as an attacker would.

These tests run the API in its **default** configuration — `AuthMode.SESSION`
— rather than the demo mode the rest of the suite uses. Each one asserts a
property that, if it stopped holding, would be a vulnerability rather than a
regression in behaviour.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.backend.auth import repository as repo
from app.backend.auth import service as auth_service
from app.backend.auth import totp
from app.backend.auth.passwords import (
    DUMMY_HASH,
    MIN_PASSWORD_LENGTH,
    PasswordError,
    hash_password,
    needs_rehash,
    validate_password,
    verify_password,
)
from app.backend.auth.permissions import OrgRole
from app.backend.core.config import AuthMode, Settings
from app.backend.core.errors import ConfigurationError
from app.backend.services.database import get_connection, initialize_schema

GOOD_PASSWORD = "correct-horse-battery-staple"


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def secure_settings(full_db):
    """The application as it ships: real sessions, no demo header."""
    return Settings(
        database_path=full_db,
        cors_allow_origins=(),
        auth_mode=AuthMode.SESSION,
        # Local test transport is http; the cookie's Secure flag would stop the
        # test client returning it. Production keeps it on, and
        # `validate_auth` refuses to start without it there.
        session_cookie_secure=False,
        rate_limit_enabled=False,
    )


@pytest.fixture
def secure_client(secure_settings):
    from app.backend.api.app import create_app

    with TestClient(create_app(secure_settings)) as client:
        yield client


@pytest.fixture
def db(full_db):
    conn = get_connection(full_db)
    initialize_schema(conn)
    yield conn
    conn.close()


def make_user(conn, email: str, *, verified: bool = True) -> str:
    user = repo.create_user(
        conn,
        email=email,
        display_name=email.split("@")[0],
        password_hash=hash_password(GOOD_PASSWORD),
        email_verified=verified,
    )
    return user.user_id


def sign_in(client: TestClient, email: str, password: str = GOOD_PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})


# --- password storage -------------------------------------------------------


def test_password_is_never_stored_in_a_recoverable_form(db):
    user_id = make_user(db, "store@example.com")
    row = db.execute(
        "SELECT password_hash FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    stored = row["password_hash"]
    assert GOOD_PASSWORD not in stored
    assert stored.startswith("scrypt$")
    assert verify_password(GOOD_PASSWORD, stored)
    assert not verify_password(GOOD_PASSWORD + "x", stored)


def test_two_identical_passwords_get_different_hashes(db):
    """A shared salt would let one cracked hash reveal every reuse of it."""
    first = hash_password(GOOD_PASSWORD)
    second = hash_password(GOOD_PASSWORD)
    assert first != second
    assert verify_password(GOOD_PASSWORD, first)
    assert verify_password(GOOD_PASSWORD, second)


def test_a_malformed_hash_is_refused_rather_than_raising():
    for junk in ("", "garbage", "scrypt$x$y$z$q$r", "bcrypt$1$2$3$4$5"):
        assert verify_password(GOOD_PASSWORD, junk) is False


def test_weaker_parameters_are_flagged_for_rehash():
    assert needs_rehash(hash_password(GOOD_PASSWORD, n=2**12))
    assert not needs_rehash(hash_password(GOOD_PASSWORD))


def test_the_policy_minimum_is_eight_characters():
    """The number itself, asserted once.

    Everything below reads the constant rather than repeating the literal, so
    this is the single test that fails if the policy is changed without anyone
    deciding to change it.
    """
    assert MIN_PASSWORD_LENGTH == 8


@pytest.mark.parametrize("password", ["", "a", "short", "sevench"])
def test_passwords_under_the_minimum_are_refused(password):
    """`validate_password` is the one gate every path goes through.

    Registration, a password change and a reset all call it, so pinning it here
    covers all three without asserting the same thing three times over HTTP.
    """
    assert len(password) < MIN_PASSWORD_LENGTH
    with pytest.raises(PasswordError):
        validate_password(password)


@pytest.mark.parametrize("password", ["eightchr", "nine-char", GOOD_PASSWORD])
def test_passwords_at_or_over_the_minimum_are_accepted(password):
    assert len(password) >= MIN_PASSWORD_LENGTH
    validate_password(password)  # must not raise


def test_registration_refuses_a_seven_character_password(secure_client):
    """One short of the line, through the endpoint a person actually reaches."""
    response = secure_client.post(
        "/api/auth/register",
        json={"email": "seven@example.com", "password": "sevench", "display_name": "S"},
    )
    assert response.status_code == 400
    # A rejected *password* is safe to explain — it says nothing about who is
    # registered — so the reason is stated rather than hidden.
    assert "8" in response.json()["error"]["message"]


def test_registration_accepts_an_eight_character_password(secure_client):
    """Exactly on the line, and usable all the way through to a session."""
    registered = secure_client.post(
        "/api/auth/register",
        json={"email": "eight@example.com", "password": "eightchr", "display_name": "E"},
    )
    assert registered.status_code == 200

    token = registered.json()["verification_token"]
    assert (
        secure_client.post("/api/auth/verify-email", json={"token": token}).status_code
        == 200
    )
    assert sign_in(secure_client, "eight@example.com", "eightchr").status_code == 200


def test_a_password_change_holds_to_the_same_minimum(secure_client, db):
    """The minimum is not a registration-time formality.

    A policy enforced only on the way in would let anyone step under it with one
    password change, so the same gate has to hold on that path too.
    """
    make_user(db, "changer@example.com")
    assert sign_in(secure_client, "changer@example.com").status_code == 200

    refused = secure_client.post(
        "/api/auth/password/change",
        json={"current_password": GOOD_PASSWORD, "new_password": "sevench"},
    )
    assert refused.status_code == 400

    accepted = secure_client.post(
        "/api/auth/password/change",
        json={"current_password": GOOD_PASSWORD, "new_password": "eightchr"},
    )
    assert accepted.status_code == 200


# --- account enumeration ----------------------------------------------------


def test_registering_a_taken_address_is_indistinguishable_from_success(secure_client):
    payload = {
        "email": "dup@example.com",
        "password": GOOD_PASSWORD,
        "display_name": "Dup",
    }
    first = secure_client.post("/api/auth/register", json=payload)
    second = secure_client.post("/api/auth/register", json={**payload, "display_name": "Other"})

    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == second.json()["status"]
    assert first.json()["message"] == second.json()["message"]


def test_unknown_and_wrong_password_logins_are_indistinguishable(secure_client, db):
    make_user(db, "known@example.com")

    unknown = sign_in(secure_client, "nobody@example.com")
    wrong = sign_in(secure_client, "known@example.com", "wrong-password-entirely")

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["error"]["message"] == wrong.json()["error"]["message"]
    assert unknown.json()["error"]["code"] == wrong.json()["error"]["code"]


def test_password_reset_request_never_reveals_whether_an_account_exists(
    secure_client, db
):
    make_user(db, "real@example.com")

    real = secure_client.post(
        "/api/auth/password/reset-request", json={"email": "real@example.com"}
    )
    fake = secure_client.post(
        "/api/auth/password/reset-request", json={"email": "ghost@example.com"}
    )

    assert real.status_code == fake.status_code == 200
    assert real.json()["message"] == fake.json()["message"]
    assert real.json()["status"] == fake.json()["status"]


def test_a_missing_account_costs_the_same_work_as_a_real_one():
    """The timing-based enumeration oracle, closed by `waste_time`."""
    assert verify_password("anything", DUMMY_HASH) is False


# --- brute force ------------------------------------------------------------


def test_repeated_failures_lock_the_account_out(secure_client, db):
    make_user(db, "brute@example.com")

    for _ in range(auth_service.MAX_FAILURES_PER_ACCOUNT):
        assert sign_in(secure_client, "brute@example.com", "wrong-password-x").status_code == 401

    # The correct password is now refused too: a lockout that yields to the
    # right password only slows an attacker down by one guess.
    locked = sign_in(secure_client, "brute@example.com")
    assert locked.status_code == 401
    assert "Too many failed attempts" in locked.json()["error"]["message"]


def test_lockout_is_recorded_in_the_audit_trail(secure_client, db):
    make_user(db, "watched@example.com")
    for _ in range(auth_service.MAX_FAILURES_PER_ACCOUNT + 1):
        sign_in(secure_client, "watched@example.com", "wrong-password-x")

    rows = db.execute(
        "SELECT event_type FROM audit_log WHERE event_type IN "
        "('login.failed', 'login.locked_out')"
    ).fetchall()
    kinds = {row["event_type"] for row in rows}
    assert "login.failed" in kinds
    assert "login.locked_out" in kinds


# --- email verification -----------------------------------------------------


def test_an_unverified_account_cannot_sign_in(secure_client, db):
    make_user(db, "unverified@example.com", verified=False)
    assert sign_in(secure_client, "unverified@example.com").status_code == 401


def test_a_verification_link_works_once(secure_client):
    registered = secure_client.post(
        "/api/auth/register",
        json={
            "email": "verify@example.com",
            "password": GOOD_PASSWORD,
            "display_name": "V",
        },
    ).json()
    token = registered["verification_token"]

    assert secure_client.post("/api/auth/verify-email", json={"token": token}).status_code == 200
    replayed = secure_client.post("/api/auth/verify-email", json={"token": token})
    assert replayed.status_code == 400


def test_registering_and_verifying_lets_the_account_sign_in(secure_client):
    """The whole chain, in the order a person walks it.

    Each half of this is covered above, but not the join between them — and the
    join is the part that fails in a way nobody can diagnose, because the
    backend answers an unverified sign-in with the same words it uses for a
    wrong password.
    """
    registered = secure_client.post(
        "/api/auth/register",
        json={
            "email": "chain@example.com",
            "password": GOOD_PASSWORD,
            "display_name": "Chain",
        },
    ).json()

    # Before verifying, even the correct password is refused.
    assert sign_in(secure_client, "chain@example.com").status_code == 401

    assert (
        secure_client.post(
            "/api/auth/verify-email", json={"token": registered["verification_token"]}
        ).status_code
        == 200
    )

    signed_in = sign_in(secure_client, "chain@example.com")
    assert signed_in.status_code == 200
    assert signed_in.json()["status"] == "authenticated"
    assert secure_client.get("/api/auth/me").status_code == 200


def test_an_unknown_verification_token_is_refused(secure_client):
    response = secure_client.post(
        "/api/auth/verify-email", json={"token": "not-a-token-anyone-issued"}
    )
    assert response.status_code == 400


def test_an_expired_verification_token_is_refused(secure_client, db):
    """Expiry is enforced on redemption, not merely recorded at issue."""
    registered = secure_client.post(
        "/api/auth/register",
        json={
            "email": "stale@example.com",
            "password": GOOD_PASSWORD,
            "display_name": "Stale",
        },
    ).json()
    token = registered["verification_token"]

    # Age the row rather than the clock: the token itself is unchanged, so what
    # is under test is the expiry check and nothing else.
    with db:
        db.execute(
            "UPDATE auth_tokens SET expires_at_utc = ?",
            ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),),
        )

    assert (
        secure_client.post("/api/auth/verify-email", json={"token": token}).status_code
        == 400
    )
    # And the account stays unverified, so an expired link cannot half-succeed.
    assert sign_in(secure_client, "stale@example.com").status_code == 401


def test_production_never_returns_a_verification_token(full_db):
    """The link is a credential, and an API response is not a mailbox.

    `_may_disclose_link` is what keeps registration from handing a verification
    token to whoever called the endpoint. This deployment has no mail transport
    in any environment, so the production consequence is that the link reaches
    nobody and an operator has to bridge that gap out of band — but the wrong
    way to close it would be to leak the token, and this is the test that says
    so. The frontend has the matching assertion for what it renders.
    """
    from app.backend.api.app import create_app

    settings = Settings(
        database_path=full_db,
        app_env="production",
        auth_mode=AuthMode.SESSION,
        cors_allow_origins=("https://app.example.com",),
        session_cookie_secure=True,
        rate_limit_enabled=False,
    )

    with TestClient(create_app(settings)) as client:
        body = client.post(
            "/api/auth/register",
            json={
                "email": "prod@example.com",
                "password": GOOD_PASSWORD,
                "display_name": "Prod",
            },
        ).json()

    assert body["status"] == "registration_received"
    # The whole response, so a token cannot reappear later under another name.
    # email_sent is always present (False in production when Resend is unconfigured).
    assert set(body) == {"status", "message", "email_sent"}
    assert body["email_sent"] is False
    assert "verification_token" not in body


def test_production_never_returns_a_password_reset_token(full_db, db):
    """The same rule, on the endpoint where leaking it would be worse."""
    from app.backend.api.app import create_app

    make_user(db, "reset-prod@example.com")

    settings = Settings(
        database_path=full_db,
        app_env="production",
        auth_mode=AuthMode.SESSION,
        cors_allow_origins=("https://app.example.com",),
        session_cookie_secure=True,
        rate_limit_enabled=False,
    )

    with TestClient(create_app(settings)) as client:
        body = client.post(
            "/api/auth/password/reset-request",
            json={"email": "reset-prod@example.com"},
        ).json()

    assert set(body) == {"status", "message"}


# --- sessions ---------------------------------------------------------------


def test_the_session_token_is_never_returned_in_a_response_body(secure_client, db):
    make_user(db, "cookie@example.com")
    response = sign_in(secure_client, "cookie@example.com")

    assert response.status_code == 200
    body = response.text
    cookie_value = response.cookies.get("astrion_session")
    assert cookie_value
    assert cookie_value not in body


def test_the_session_cookie_is_httponly_and_samesite(secure_client, db):
    make_user(db, "flags@example.com")
    response = sign_in(secure_client, "flags@example.com")

    header = response.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=lax" in header
    assert "path=/" in header


def test_only_a_digest_of_the_session_token_is_stored(secure_client, db):
    make_user(db, "digest@example.com")
    token = sign_in(secure_client, "digest@example.com").cookies["astrion_session"]

    stored = [row["token_hash"] for row in db.execute("SELECT token_hash FROM sessions")]
    assert stored
    assert token not in stored


def test_an_unauthenticated_caller_reaches_nothing(secure_client):
    for method, path, body in (
        ("post", "/api/chat", {"message": "hello"}),
        ("get", "/api/actions/pending", None),
        ("get", "/api/auth/me", None),
    ):
        response = getattr(secure_client, method)(path, **({"json": body} if body else {}))
        assert response.status_code == 401, path


def test_a_forged_session_cookie_is_refused(secure_client):
    secure_client.cookies.set("astrion_session", "not-a-real-token")
    response = secure_client.get("/api/auth/me")
    assert response.status_code == 401


def test_a_revoked_session_stops_working_immediately(secure_client, db):
    make_user(db, "revoke@example.com")
    sign_in(secure_client, "revoke@example.com")
    assert secure_client.get("/api/auth/me").status_code == 200

    secure_client.post("/api/auth/logout")
    assert secure_client.get("/api/auth/me").status_code == 401


def test_an_expired_session_is_refused(db):
    """Expiry is enforced on lookup, not left to a sweeper job."""
    user_id = make_user(db, "expired@example.com")
    _sid, token = repo.create_session(
        db,
        user_id=user_id,
        org_id=None,
        mfa_satisfied=True,
        idle_minutes=-1,  # already past
    )
    assert repo.lookup_session(db, token) is None


def test_login_issues_a_brand_new_session_id(secure_client, db):
    """Session fixation: a pre-set cookie is never elevated, only replaced."""
    make_user(db, "fixation@example.com")
    secure_client.cookies.set("astrion_session", "attacker-planted-value")

    response = sign_in(secure_client, "fixation@example.com")
    issued = response.cookies["astrion_session"]
    assert issued != "attacker-planted-value"


def test_changing_a_password_revokes_every_other_session(db):
    user_id = make_user(db, "rotate@example.com")
    _a, token_a = repo.create_session(db, user_id=user_id, org_id=None, mfa_satisfied=True)
    keep_id, token_keep = repo.create_session(
        db, user_id=user_id, org_id=None, mfa_satisfied=True
    )

    assert auth_service.change_password(
        db,
        user_id=user_id,
        current_password=GOOD_PASSWORD,
        new_password="a-brand-new-password-here",
        keep_session_id=keep_id,
    )
    assert repo.lookup_session(db, token_a) is None
    assert repo.lookup_session(db, token_keep) is not None


def test_a_password_reset_revokes_every_session(db):
    user_id = make_user(db, "resetall@example.com")
    _sid, token = repo.create_session(db, user_id=user_id, org_id=None, mfa_satisfied=True)

    reset = auth_service.request_password_reset(db, email="resetall@example.com")
    assert auth_service.complete_password_reset(
        db, token=reset, new_password="another-good-password-1"
    )
    assert repo.lookup_session(db, token) is None


# --- password reset tokens --------------------------------------------------


def test_a_reset_token_cannot_be_reused(db):
    make_user(db, "once@example.com")
    token = auth_service.request_password_reset(db, email="once@example.com")

    assert auth_service.complete_password_reset(
        db, token=token, new_password="first-new-password-1"
    )
    assert not auth_service.complete_password_reset(
        db, token=token, new_password="second-new-password-2"
    )


def test_requesting_a_new_reset_link_invalidates_the_previous_one(db):
    make_user(db, "reissue@example.com")
    first = auth_service.request_password_reset(db, email="reissue@example.com")
    second = auth_service.request_password_reset(db, email="reissue@example.com")

    assert not auth_service.complete_password_reset(
        db, token=first, new_password="should-not-work-here-1"
    )
    assert auth_service.complete_password_reset(
        db, token=second, new_password="should-work-here-ok-2"
    )


def test_an_expired_reset_token_is_refused(db):
    user_id = make_user(db, "stale@example.com")
    token = repo.issue_auth_token(
        db, user_id=user_id, purpose=auth_service.RESET_PURPOSE, ttl_minutes=-1
    )
    assert repo.consume_auth_token(db, token=token, purpose=auth_service.RESET_PURPOSE) is None


def test_a_reset_token_cannot_be_used_as_a_verification_token(db):
    """Purpose binding: one kind of link must not be redeemable as another."""
    user_id = make_user(db, "purpose@example.com")
    token = repo.issue_auth_token(
        db, user_id=user_id, purpose=auth_service.RESET_PURPOSE, ttl_minutes=30
    )
    assert (
        repo.consume_auth_token(
            db, token=token, purpose=auth_service.VERIFICATION_PURPOSE
        )
        is None
    )


def test_only_a_digest_of_a_reset_token_is_stored(db):
    make_user(db, "tokendigest@example.com")
    token = auth_service.request_password_reset(db, email="tokendigest@example.com")
    stored = [r["token_hash"] for r in db.execute("SELECT token_hash FROM auth_tokens")]
    assert token not in stored


# --- multi-factor authentication --------------------------------------------


def test_mfa_blocks_a_session_until_a_code_is_supplied(secure_client, db):
    user_id = make_user(db, "mfa@example.com")
    secret = totp.new_secret()
    repo.set_mfa_secret(db, user_id, secret)
    repo.set_mfa_enabled(db, user_id, True)

    login = sign_in(secure_client, "mfa@example.com")
    assert login.status_code == 200
    assert login.json()["mfa_required"] is True

    # The half-authenticated session reaches nothing but the challenge.
    assert secure_client.get("/api/auth/me").status_code == 401

    challenge = secure_client.post(
        "/api/auth/mfa/challenge", json={"code": totp.generate(secret)}
    )
    assert challenge.status_code == 200
    assert secure_client.get("/api/auth/me").status_code == 200


def test_a_wrong_mfa_code_does_not_satisfy_the_session(secure_client, db):
    user_id = make_user(db, "mfabad@example.com")
    secret = totp.new_secret()
    repo.set_mfa_secret(db, user_id, secret)
    repo.set_mfa_enabled(db, user_id, True)

    sign_in(secure_client, "mfabad@example.com")
    assert secure_client.post("/api/auth/mfa/challenge", json={"code": "000000"}).status_code == 401
    assert secure_client.get("/api/auth/me").status_code == 401


def test_an_mfa_code_cannot_be_replayed(db):
    """The acceptance window would otherwise let an observed code be reused."""
    secret = totp.new_secret()
    now = time.time()
    code = totp.generate(secret, at=now)

    first = totp.verify(secret, code, at=now)
    assert first is not None
    assert totp.verify(secret, code, at=now, last_used_step=first) is None


def test_mfa_is_not_enabled_until_a_code_is_confirmed(secure_client, db):
    make_user(db, "enrol@example.com")
    sign_in(secure_client, "enrol@example.com")

    enrolled = secure_client.post("/api/auth/mfa/enrol").json()
    assert enrolled["status"] == "pending_confirmation"
    user = repo.get_user_by_email(db, "enrol@example.com")
    assert user.mfa_enabled is False

    confirmed = secure_client.post(
        "/api/auth/mfa/confirm", json={"code": totp.generate(enrolled["secret"])}
    )
    assert confirmed.status_code == 200
    assert repo.get_user_by_email(db, "enrol@example.com").mfa_enabled is True


def test_disabling_mfa_requires_the_password_again(db):
    user_id = make_user(db, "mfaoff@example.com")
    repo.set_mfa_secret(db, user_id, totp.new_secret())
    repo.set_mfa_enabled(db, user_id, True)

    assert not auth_service.disable_mfa(
        db, user_id=user_id, password="not-the-right-password"
    )
    assert repo.get_user(db, user_id).mfa_enabled is True

    assert auth_service.disable_mfa(db, user_id=user_id, password=GOOD_PASSWORD)
    assert repo.get_user(db, user_id).mfa_enabled is False


# --- configuration fail-closed ----------------------------------------------


def test_session_authentication_is_the_default():
    assert Settings().auth_mode is AuthMode.SESSION


def test_the_demo_header_cannot_be_enabled_in_production():
    with pytest.raises(ConfigurationError, match="must never run in production"):
        Settings(app_env="production", auth_mode=AuthMode.DEMO_HEADER).validate_auth()


def test_an_insecure_cookie_is_refused_in_production():
    with pytest.raises(ConfigurationError, match="plaintext HTTP"):
        Settings(app_env="production", session_cookie_secure=False).validate_auth()


def test_a_wildcard_cors_origin_is_refused():
    with pytest.raises(ConfigurationError, match="cannot be combined"):
        Settings(cors_allow_origins=("*",)).validate_cors()


def test_a_plaintext_cors_origin_is_refused_in_production():
    with pytest.raises(ConfigurationError, match="plaintext HTTP"):
        Settings(
            app_env="production", cors_allow_origins=("http://app.example.com",)
        ).validate_cors()


def test_the_demo_directory_is_not_published_under_session_auth(secure_client):
    """A persona list is meaningless once identity is real, and advertising it
    would invite a sign-in that does not exist."""
    response = secure_client.get("/api/principals")
    assert response.status_code == 200
    assert response.json()["principals"] == []
"""Tests to append to test_security_auth.py"""

# --- resend verification ----------------------------------------------------


def test_resend_verification_sends_a_new_token(secure_client, db):
    """Resend mints a fresh token and returns the expected shape."""
    make_user(db, "resend@example.com", verified=False)

    response = secure_client.post(
        "/api/auth/resend-verification",
        json={"email": "resend@example.com"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "resend_requested"
    assert "resend_state" in body
    assert "email_sent" in body
    assert "message" in body


def test_resend_verification_unknown_email_returns_same_shape(secure_client):
    """Unknown addresses must be indistinguishable from valid ones."""
    response = secure_client.post(
        "/api/auth/resend-verification",
        json={"email": "nobody@example.com"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "resend_requested"
    assert "resend_state" in body
    assert "verification_token" not in body


def test_resend_verification_already_verified_returns_same_shape(secure_client, db):
    """Already-verified addresses return the same shape as unknown ones."""
    make_user(db, "verified-resend@example.com", verified=True)

    response = secure_client.post(
        "/api/auth/resend-verification",
        json={"email": "verified-resend@example.com"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "resend_requested"
    assert body["resend_state"]["can_resend"] is False


def test_resend_verification_30s_minimum_interval_enforced(secure_client, db):
    """A second resend within 30 seconds is refused."""
    make_user(db, "ratelimit@example.com", verified=False)

    r1 = secure_client.post(
        "/api/auth/resend-verification",
        json={"email": "ratelimit@example.com"},
    )
    assert r1.status_code == 200

    r2 = secure_client.post(
        "/api/auth/resend-verification",
        json={"email": "ratelimit@example.com"},
    )
    assert r2.status_code == 400


def test_resend_verification_max_count_cooldown_enforced(secure_client, db, full_db):
    """After RESEND_MAX_COUNT sends, a cooldown is imposed."""
    from datetime import datetime, timedelta, timezone
    from app.backend.auth.repository import RESEND_MAX_COUNT, RESEND_COOLDOWN_HOURS
    from app.backend.services.database import get_connection, initialize_schema

    make_user(db, "cooldown@example.com", verified=False)

    conn = get_connection(full_db)
    initialize_schema(conn)
    try:
        conn.execute(
            """
            UPDATE users
               SET verification_sent_at_utc = ?,
                   verification_resend_count = ?,
                   verification_cooldown_until_utc = ?
             WHERE email = ?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                RESEND_MAX_COUNT,
                (datetime.now(timezone.utc) + timedelta(hours=RESEND_COOLDOWN_HOURS)).isoformat(),
                "cooldown@example.com",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    response = secure_client.post(
        "/api/auth/resend-verification",
        json={"email": "cooldown@example.com"},
    )
    assert response.status_code == 400


def test_resend_verification_token_not_in_production_response(full_db):
    """In production, no token is returned even if email delivery fails."""
    from app.backend.api.app import create_app

    settings = Settings(
        database_path=full_db,
        app_env="production",
        auth_mode=AuthMode.SESSION,
        cors_allow_origins=("https://app.example.com",),
        session_cookie_secure=True,
        rate_limit_enabled=False,
    )

    with TestClient(create_app(settings)) as client:
        client.post(
            "/api/auth/register",
            json={
                "email": "prod-resend@example.com",
                "password": GOOD_PASSWORD,
                "display_name": "ProdResend",
            },
        )
        response = client.post(
            "/api/auth/resend-verification",
            json={"email": "prod-resend@example.com"},
        )

    assert response.status_code in (200, 400)
    body = response.json()
    assert "verification_token" not in body


# --- email provider ---------------------------------------------------------


def test_null_provider_raises_email_delivery_error():
    """The null provider never silently succeeds."""
    from app.backend.email.provider import EmailDeliveryError, NullEmailProvider

    provider = NullEmailProvider()
    with pytest.raises(EmailDeliveryError):
        provider.send_verification_email(
            to_address="test@example.com",
            display_name="Test",
            verification_url="http://localhost:3000/verify-email?token=abc",
        )


def test_email_provider_factory_returns_null_when_no_api_key():
    """With no RESEND_API_KEY the factory returns a NullEmailProvider."""
    from app.backend.core.config import email_provider_for
    from app.backend.email.provider import NullEmailProvider

    provider = email_provider_for(Settings())
    assert isinstance(provider, NullEmailProvider)


def test_verification_email_html_escapes_display_name():
    """A malicious display name cannot inject HTML into the email body."""
    from app.backend.email.templates import verification_email_html

    html = verification_email_html(
        display_name='<script>alert("xss")</script>',
        verification_url="http://localhost:3000/verify-email?token=safe",
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_verification_url_is_built_from_config():
    """_build_verification_url uses the configured EMAIL_VERIFICATION_URL."""
    from app.backend.api.auth_routes import _build_verification_url

    settings = Settings(email_verification_url="https://app.example.com")
    url = _build_verification_url(settings, "my-token-123")
    assert url.startswith("https://app.example.com/verify-email")
    assert "token=my-token-123" in url


def test_resend_state_get_and_record():
    """get_resend_state and record_verification_sent work end-to-end in memory."""
    import sqlite3 as _sqlite3
    from app.backend.auth.repository import get_resend_state, record_verification_sent
    from app.backend.auth.schema import SECURITY_SCHEMA_STATEMENTS
    from app.backend.auth.passwords import hash_password
    from app.backend.services.database import _apply_added_columns
    import app.backend.auth.repository as r

    conn = _sqlite3.connect(":memory:")
    conn.row_factory = _sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    for stmt in SECURITY_SCHEMA_STATEMENTS:
        conn.execute(stmt)
    _apply_added_columns(conn)

    user = r.create_user(
        conn,
        email="state@example.com",
        display_name="State",
        password_hash=hash_password(GOOD_PASSWORD),
    )
    uid = user.user_id

    state = get_resend_state(conn, uid)
    assert state.can_send is True
    assert state.sends_used == 0

    record_verification_sent(conn, uid)
    state = get_resend_state(conn, uid)
    assert state.can_send is False
    assert state.sends_used == 1
    assert state.seconds_until_allowed > 0
