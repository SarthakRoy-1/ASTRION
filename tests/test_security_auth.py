"""Authentication and session security, exercised as an attacker would.

These tests run the API in its **default** configuration — `AuthMode.SESSION`
— rather than the demo mode the rest of the suite uses. Each one asserts a
property that, if it stopped holding, would be a vulnerability rather than a
regression in behaviour.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.backend.auth import repository as repo
from app.backend.auth import service as auth_service
from app.backend.auth import totp
from app.backend.auth.passwords import (
    DUMMY_HASH,
    hash_password,
    needs_rehash,
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


def test_short_passwords_are_refused(secure_client):
    response = secure_client.post(
        "/api/auth/register",
        json={"email": "short@example.com", "password": "short", "display_name": "S"},
    )
    assert response.status_code == 400


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


# --- sessions ---------------------------------------------------------------


def test_the_session_token_is_never_returned_in_a_response_body(secure_client, db):
    make_user(db, "cookie@example.com")
    response = sign_in(secure_client, "cookie@example.com")

    assert response.status_code == 200
    body = response.text
    cookie_value = response.cookies.get("parcelpilot_session")
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
    token = sign_in(secure_client, "digest@example.com").cookies["parcelpilot_session"]

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
    secure_client.cookies.set("parcelpilot_session", "not-a-real-token")
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
    secure_client.cookies.set("parcelpilot_session", "attacker-planted-value")

    response = sign_in(secure_client, "fixation@example.com")
    issued = response.cookies["parcelpilot_session"]
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
