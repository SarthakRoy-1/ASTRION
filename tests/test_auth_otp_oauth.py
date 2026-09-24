"""Email one-time codes and sign-in with Google / GitHub, exercised as an attacker would.

The provider side is simulated with `httpx.MockTransport`, installed on
`app.state.oauth_http`: no test reaches Google or GitHub, and every request the
backend would make to them is asserted on instead.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app.backend.auth import repository as repo
from app.backend.auth import totp
from app.backend.auth.passwords import UNUSABLE_PASSWORD, hash_password, verify_password
from app.backend.auth.verification import mask_email
from app.backend.core.config import AuthMode, Settings
from app.backend.core.errors import ConfigurationError
from app.backend.services.database import get_connection, initialize_schema

GOOD_PASSWORD = "correct-horse-battery-staple"
FRONTEND = "https://app.example.com"
API = "https://api.example.com"


# --- fixtures -----------------------------------------------------------------


class CapturingEmailProvider:
    def __init__(self) -> None:
        self.codes: list[tuple[str, str]] = []
        self.fail = False

    def send_verification_email(self, *, to_address, display_name, verification_url):
        raise AssertionError("no verification links are sent any more")

    def send_verification_code(self, *, to_address, display_name, code, expires_minutes):
        from app.backend.email.provider import EmailDeliveryError

        if self.fail:
            raise EmailDeliveryError("simulated outage")
        self.codes.append((to_address, code))

    def codes_for(self, address: str) -> list[str]:
        return [code for to, code in self.codes if to == address]


class FakeProvider:
    """Google and GitHub, as far as the backend's HTTP calls can tell."""

    def __init__(self) -> None:
        self.google_profile: dict = {}
        self.github_user: dict = {}
        self.github_emails: list = []
        self.token_status = 200
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url.endswith("/token") or url.endswith("/access_token"):
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "provider-access-token-secret"})
        assert request.headers["authorization"] == "Bearer provider-access-token-secret"
        if "openidconnect" in url:
            return httpx.Response(200, json=self.google_profile)
        if url.endswith("/user/emails"):
            return httpx.Response(200, json=self.github_emails)
        if url.endswith("/user"):
            return httpx.Response(200, json=self.github_user)
        return httpx.Response(404)


def _settings(full_db, **overrides) -> Settings:
    base = dict(
        database_path=full_db,
        cors_allow_origins=(),
        auth_mode=AuthMode.SESSION,
        session_cookie_secure=False,
        rate_limit_enabled=False,
        frontend_base_url=FRONTEND,
        api_public_url=API,
        google_oauth_client_id="google-client-id",
        google_oauth_client_secret="google-client-secret-value",
        github_oauth_client_id="github-client-id",
        github_oauth_client_secret="github-client-secret-value",
    )
    base.update(overrides)
    return Settings(**base)


def _client(settings: Settings):
    from app.backend.api.app import create_app

    app = create_app(settings)
    app.state.email_provider = CapturingEmailProvider()
    provider = FakeProvider()
    app.state.oauth_http = httpx.Client(transport=httpx.MockTransport(provider.handler))
    return app, provider


@pytest.fixture
def env(full_db):
    app, provider = _client(_settings(full_db))
    with TestClient(app) as client:
        client.provider = provider  # type: ignore[attr-defined]
        client.mail = app.state.email_provider  # type: ignore[attr-defined]
        yield client


@pytest.fixture
def db(full_db):
    conn = get_connection(full_db)
    initialize_schema(conn)
    yield conn
    conn.close()


def make_user(conn, email: str, *, verified: bool = True, password: str = GOOD_PASSWORD):
    return repo.create_user(
        conn,
        email=email,
        display_name=email.split("@")[0],
        password_hash=hash_password(password),
        email_verified=verified,
    )


def register(client, email: str, password: str = GOOD_PASSWORD):
    return client.post(
        "/api/auth/register",
        json={"email": email, "password": password, "display_name": "New Person"},
    )


def verify(client, code: str):
    return client.post("/api/auth/verification/verify", json={"code": code})


def wrong_code(right: str) -> str:
    return f"{(int(right) + 1) % 1_000_000:06d}"


# --- registration and the emailed code -------------------------------------------


def test_registration_emails_a_six_digit_code_and_never_returns_it(env):
    response = register(env, "fresh@example.com")
    assert response.status_code == 200

    codes = env.mail.codes_for("fresh@example.com")
    assert len(codes) == 1
    code = codes[0]
    assert len(code) == 6 and code.isdigit()

    body = response.json()
    assert body["email_sent"] is True
    assert body["verification"]["email_hint"] == "f••••@example.com"
    assert body["verification"]["needs_email"] is False
    # The code appears nowhere in what the browser receives.
    assert code not in response.text
    assert all(code not in value for value in response.headers.values())


def test_the_code_is_stored_only_as_a_salted_hash(env, db):
    register(env, "hashed@example.com")
    code = env.mail.codes_for("hashed@example.com")[0]
    rows = db.execute("SELECT * FROM email_otps").fetchall()
    assert rows
    for row in rows:
        assert code not in json.dumps(dict(row))


def test_the_correct_code_verifies_the_address_and_starts_a_session(env, db):
    register(env, "right@example.com")
    code = env.mail.codes_for("right@example.com")[0]

    response = verify(env, code)
    assert response.status_code == 200
    assert response.json()["status"] == "authenticated"
    assert repo.get_user_by_email(db, "right@example.com").email_verified

    me = env.get("/api/auth/me")
    assert me.status_code == 200
    # Registered, verified, no workspace yet: the existing onboarding follows.
    assert env.get("/api/workspaces").json()["needs_workspace"] is True


def test_an_incorrect_code_is_refused_and_counts_down(env, db):
    register(env, "wrong@example.com")
    code = env.mail.codes_for("wrong@example.com")[0]

    response = verify(env, wrong_code(code))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "otp_invalid"
    assert response.json()["error"]["details"]["attempts_remaining"] == 4
    assert not repo.get_user_by_email(db, "wrong@example.com").email_verified
    assert env.get("/api/auth/me").status_code == 401


def test_malformed_input_spends_an_attempt_too(env):
    register(env, "malformed@example.com")
    response = verify(env, "abc")
    assert response.json()["error"]["code"] == "otp_invalid"
    assert response.json()["error"]["details"]["attempts_remaining"] == 4


def test_pasted_codes_with_spaces_are_accepted(env):
    register(env, "paste@example.com")
    code = env.mail.codes_for("paste@example.com")[0]
    assert verify(env, f" {code[:3]} {code[3:]} ").status_code == 200


def test_an_expired_code_is_refused(env, db):
    register(env, "late@example.com")
    code = env.mail.codes_for("late@example.com")[0]
    with db:
        db.execute(
            "UPDATE email_otps SET expires_at_utc = ?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
        )
    response = verify(env, code)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "otp_expired"
    assert not repo.get_user_by_email(db, "late@example.com").email_verified


def test_a_code_cannot_be_used_twice(env):
    register(env, "once@example.com")
    code = env.mail.codes_for("once@example.com")[0]
    assert verify(env, code).status_code == 200
    replay = verify(env, code)
    assert replay.status_code == 400
    assert replay.json()["error"]["code"] == "verification_expired"


def test_the_attempt_limit_burns_the_code_even_for_the_right_answer(env, db):
    register(env, "brute@example.com")
    code = env.mail.codes_for("brute@example.com")[0]
    codes = [verify(env, wrong_code(code)).json()["error"]["code"] for _ in range(5)]
    assert codes[:4] == ["otp_invalid"] * 4
    assert codes[4] == "otp_attempts_exceeded"

    final = verify(env, code)
    assert final.json()["error"]["code"] == "otp_attempts_exceeded"
    assert not repo.get_user_by_email(db, "brute@example.com").email_verified


def test_attempt_limit_is_configurable(full_db):
    app, _ = _client(_settings(full_db, otp_max_attempts=2))
    with TestClient(app) as client:
        register(client, "two@example.com")
        code = app.state.email_provider.codes_for("two@example.com")[0]
        assert verify(client, wrong_code(code)).json()["error"]["code"] == "otp_invalid"
        assert (
            verify(client, wrong_code(code)).json()["error"]["code"]
            == "otp_attempts_exceeded"
        )


def test_the_code_lifetime_is_configurable(full_db):
    app, _ = _client(_settings(full_db, otp_ttl_minutes=3))
    with TestClient(app) as client:
        body = register(client, "ttl@example.com").json()
    assert body["verification"]["code_ttl_seconds"] == 180
    assert 0 < body["verification"]["code_expires_in_seconds"] <= 180


# --- resend -----------------------------------------------------------------------


def test_an_immediate_resend_is_refused_with_a_cooldown(env):
    register(env, "cool@example.com")
    response = env.post("/api/auth/verification/resend")
    assert response.status_code == 429
    error = response.json()["error"]
    assert error["code"] == "otp_resend_cooldown"
    assert 0 < error["details"]["retry_after_seconds"] <= 61
    assert len(env.mail.codes_for("cool@example.com")) == 1


def test_a_resend_supersedes_the_previous_code(full_db):
    app, _ = _client(_settings(full_db, otp_resend_cooldown_seconds=0))
    with TestClient(app) as client:
        register(client, "again@example.com")
        first = app.state.email_provider.codes_for("again@example.com")[0]
        resent = client.post("/api/auth/verification/resend")
        assert resent.status_code == 200
        assert resent.json()["verification"]["code_expires_in_seconds"] > 0
        second = app.state.email_provider.codes_for("again@example.com")[1]

        if first != second:
            # The first code is dead: it is checked against the newest one.
            assert verify(client, first).status_code == 400
        assert verify(client, second).status_code == 200


def test_resends_are_capped_per_window(full_db):
    app, _ = _client(
        _settings(full_db, otp_resend_cooldown_seconds=0, otp_max_sends_per_window=3)
    )
    with TestClient(app) as client:
        register(client, "cap@example.com")
        assert client.post("/api/auth/verification/resend").status_code == 200
        assert client.post("/api/auth/verification/resend").status_code == 200
        refused = client.post("/api/auth/verification/resend")
        assert refused.status_code == 429
        assert refused.json()["error"]["code"] == "otp_send_limit"
        assert len(app.state.email_provider.codes_for("cap@example.com")) == 3


def test_the_address_cap_holds_across_repeated_sign_ins(full_db, db):
    """Signing in again from a fresh browser cannot mint unlimited emails."""
    make_user(db, "spam@example.com", verified=False)
    settings = _settings(full_db, otp_resend_cooldown_seconds=0, otp_max_sends_per_window=2)
    app, _ = _client(settings)
    for _ in range(4):
        with TestClient(app) as browser:  # a new cookie jar every time
            browser.post(
                "/api/auth/login",
                json={"email": "spam@example.com", "password": GOOD_PASSWORD},
            )
    assert len(app.state.email_provider.codes_for("spam@example.com")) == 2


def test_email_delivery_failure_is_reported_and_recoverable(full_db):
    app, _ = _client(_settings(full_db, otp_resend_cooldown_seconds=0))
    with TestClient(app) as client:
        app.state.email_provider.fail = True
        body = register(client, "outage@example.com").json()
        assert body["email_sent"] is False
        assert body["verification"]["email_sent"] is False

        failed = client.post("/api/auth/verification/resend")
        assert failed.status_code == 200
        assert failed.json()["status"] == "delivery_failed"

        app.state.email_provider.fail = False
        assert client.post("/api/auth/verification/resend").json()["status"] == "code_sent"
        code = app.state.email_provider.codes_for("outage@example.com")[-1]
        assert verify(client, code).status_code == 200


# --- enumeration ------------------------------------------------------------------


def test_registering_a_taken_address_emails_nobody_and_can_never_verify(env, db):
    make_user(db, "taken@example.com")
    first = register(env, "taken@example.com")
    assert first.status_code == 200
    assert first.json()["email_sent"] is True  # indistinguishable from a real send
    assert env.mail.codes_for("taken@example.com") == []

    # Whatever is guessed, the decoy refuses exactly as a real code would.
    outcomes = [verify(env, f"{n:06d}").json()["error"]["code"] for n in range(5)]
    assert outcomes == ["otp_invalid"] * 4 + ["otp_attempts_exceeded"]
    assert env.get("/api/auth/me").status_code == 401


def test_a_taken_and_a_free_address_answer_in_the_same_shape(env, db):
    make_user(db, "exists@example.com")
    real = register(env, "brandnew@example.com").json()
    env.cookies.clear()
    decoy = register(env, "exists@example.com").json()
    assert set(real) == set(decoy)
    assert set(real["verification"]) == set(decoy["verification"])
    for key in ("purpose", "needs_email", "sends_remaining", "attempts_remaining", "email_sent"):
        assert real["verification"][key] == decoy["verification"][key]


def test_the_verify_endpoints_take_no_address(env):
    response = env.post(
        "/api/auth/verification/verify",
        json={"code": "123456", "email": "victim@example.com"},
    )
    assert response.status_code == 422


def test_without_a_verification_cookie_nothing_can_be_verified(env):
    for path in ("/api/auth/verification/verify", "/api/auth/verification/resend"):
        body = {"code": "123456"} if path.endswith("verify") else None
        response = env.post(path, json=body) if body else env.post(path)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "verification_expired"


# --- sign-in with a password ---------------------------------------------------------


def test_a_verified_account_signs_in_without_a_code(env, db):
    make_user(db, "normal@example.com")
    response = env.post(
        "/api/auth/login", json={"email": "normal@example.com", "password": GOOD_PASSWORD}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "authenticated"
    assert env.mail.codes == []


def test_an_unverified_account_with_the_right_password_is_routed_to_verification(env, db):
    make_user(db, "pending@example.com", verified=False)
    response = env.post(
        "/api/auth/login", json={"email": "pending@example.com", "password": GOOD_PASSWORD}
    )
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "email_verification_required"
    assert error["details"]["verification"]["email_hint"] == mask_email("pending@example.com")
    # Not signed in: no session was issued.
    assert env.get("/api/auth/me").status_code == 401

    # A reload finds its way back: /me says a verification is in progress.
    assert "verification" in env.get("/api/auth/me").json()["error"]["details"]

    code = env.mail.codes_for("pending@example.com")[0]
    verified = verify(env, code)
    assert verified.status_code == 200
    assert env.get("/api/auth/me").status_code == 200


def test_signing_in_twice_reuses_the_code_already_sent(env, db):
    make_user(db, "twice@example.com", verified=False)
    for _ in range(2):
        env.post("/api/auth/login", json={"email": "twice@example.com", "password": GOOD_PASSWORD})
    assert len(env.mail.codes_for("twice@example.com")) == 1


def test_a_wrong_password_on_an_unverified_account_reveals_nothing(env, db):
    make_user(db, "quiet@example.com", verified=False)
    wrong = env.post(
        "/api/auth/login", json={"email": "quiet@example.com", "password": "not-the-password"}
    )
    unknown = env.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": "not-the-password"}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"] == {**unknown.json()["error"], "request_id": wrong.json()["error"]["request_id"]}
    assert env.mail.codes == []


def test_verification_honours_an_enabled_second_factor(env, db):
    user = make_user(db, "mfa@example.com", verified=False)
    repo.set_mfa_secret(db, user.user_id, totp.new_secret())
    repo.set_mfa_enabled(db, user.user_id, True)
    env.post("/api/auth/login", json={"email": "mfa@example.com", "password": GOOD_PASSWORD})
    code = env.mail.codes_for("mfa@example.com")[0]
    response = verify(env, code)
    assert response.json()["status"] == "mfa_required"
    me = env.get("/api/auth/me")
    assert me.status_code == 401
    assert me.json()["error"]["details"].get("mfa_required") is True


# --- configuration -----------------------------------------------------------------


def test_the_development_outbox_is_refused_in_production(tmp_path):
    with pytest.raises(ConfigurationError, match="EMAIL_OUTBOX_DIR"):
        Settings(app_env="production", email_outbox_dir=tmp_path).validate_auth()


def test_the_development_outbox_writes_a_private_file(tmp_path):
    from app.backend.email.outbox_provider import OutboxEmailProvider

    OutboxEmailProvider(tmp_path).send_verification_code(
        to_address="dev@example.com", display_name="Dev", code="123456", expires_minutes=10
    )
    [written] = list(tmp_path.glob("*.json"))
    assert json.loads(written.read_text())["code"] == "123456"
    if os.name == "posix":
        assert written.stat().st_mode & 0o077 == 0


def test_the_code_email_carries_brand_purpose_expiry_and_warning():
    from app.backend.email.templates import (
        verification_code_email_html,
        verification_code_email_text,
    )

    for body in (
        verification_code_email_text(display_name="Sam", code="482913", expires_minutes=10),
        verification_code_email_html(display_name="Sam", code="482913", expires_minutes=10),
    ):
        assert "ASTRION" in body
        assert "482913" in body
        assert "10 minutes" in body
        assert "Do not share this code" in body
        assert "verify your email address" in body.lower()


def test_oauth_needs_https_urls_in_production():
    with pytest.raises(ConfigurationError, match="API_PUBLIC_URL"):
        Settings(
            app_env="production",
            google_oauth_client_id="id",
            google_oauth_client_secret="secret",
            api_public_url="http://api.example.com",
            frontend_base_url="https://app.example.com",
        ).validate_auth()


# --- Google / GitHub ---------------------------------------------------------------


def start(client, provider: str):
    response = client.get(f"/api/auth/oauth/{provider}/start", follow_redirects=False)
    assert response.status_code == 302, response.text
    location = urlparse(response.headers["location"])
    return location, parse_qs(location.query)


def callback(client, provider: str, **params):
    return client.get(
        f"/api/auth/oauth/{provider}/callback", params=params, follow_redirects=False
    )


def sign_in_with(client, provider: str, code: str = "auth-code-from-provider"):
    _, query = start(client, provider)
    return callback(client, provider, code=code, state=query["state"][0])


def test_health_lists_only_configured_providers(full_db):
    app, _ = _client(_settings(full_db, github_oauth_client_secret=None))
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["oauth_providers"] == ["google"]
    assert "secret" not in json.dumps(body)
    assert "google-client-id" not in json.dumps(body)


@pytest.mark.parametrize("provider,host", [("google", "accounts.google.com"), ("github", "github.com")])
def test_starting_a_provider_sign_in_redirects_with_state_and_pkce(env, provider, host):
    response = env.get(f"/api/auth/oauth/{provider}/start", follow_redirects=False)
    location = urlparse(response.headers["location"])
    query = parse_qs(location.query)
    assert location.hostname == host
    assert query["redirect_uri"] == [f"{API}/api/auth/oauth/{provider}/callback"]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["state"][0]) >= 32
    assert query["client_id"] == [f"{provider}-client-id"]
    # The secret is for the token exchange, server to server. Never the browser.
    assert "secret" not in response.headers["location"]
    set_cookie = response.headers["set-cookie"]
    assert "HttpOnly" in set_cookie and "SameSite=lax" in set_cookie
    assert "Path=/api/auth/oauth" in set_cookie


def test_an_unconfigured_provider_is_answered_on_the_sign_in_page(full_db):
    app, _ = _client(_settings(full_db, google_oauth_client_id=None))
    with TestClient(app) as client:
        response = client.get("/api/auth/oauth/google/start", follow_redirects=False)
    assert response.headers["location"] == f"{FRONTEND}/sign-in?auth_error=oauth_unavailable"


def test_an_unknown_provider_does_not_exist(env):
    assert env.get("/api/auth/oauth/myspace/start", follow_redirects=False).status_code == 404


def test_google_creates_a_verified_account_without_a_password(env, db):
    env.provider.google_profile = {
        "sub": "google-123", "email": "Ada@Example.com", "email_verified": True, "name": "Ada",
    }
    response = sign_in_with(env, "google")
    assert response.status_code == 302
    assert response.headers["location"] == f"{FRONTEND}/sign-in"
    # Neither the provider's code nor its access token reaches the browser.
    assert "auth-code-from-provider" not in response.headers["location"]
    assert "provider-access-token-secret" not in str(response.headers)

    user = repo.get_user_by_email(db, "ada@example.com")
    assert user is not None and user.email_verified
    assert user.display_name == "Ada"
    assert user.password_hash == UNUSABLE_PASSWORD
    assert env.get("/api/auth/me").status_code == 200
    assert env.get("/api/workspaces").json()["needs_workspace"] is True

    # The exchange sent the PKCE verifier and the registered redirect URI.
    token_request = next(r for r in env.provider.requests if str(r.url).endswith("/token"))
    form = parse_qs(token_request.content.decode())
    assert form["redirect_uri"] == [f"{API}/api/auth/oauth/google/callback"]
    assert form["code_verifier"][0]


def test_signing_in_again_with_the_same_identity_opens_the_same_account(env, db):
    env.provider.google_profile = {"sub": "g-9", "email": "same@example.com", "email_verified": True}
    sign_in_with(env, "google")
    env.post("/api/auth/logout")
    # The address at Google changed; the identity did not.
    env.provider.google_profile = {"sub": "g-9", "email": "renamed@example.com", "email_verified": True}
    sign_in_with(env, "google")
    assert env.get("/api/auth/me").json()["user_id"] == repo.get_user_by_email(
        db, "same@example.com"
    ).user_id
    assert repo.get_user_by_email(db, "renamed@example.com") is None


def test_google_links_to_an_existing_verified_password_account(env, db):
    existing = make_user(db, "linked@example.com")
    env.provider.google_profile = {"sub": "g-1", "email": "linked@example.com", "email_verified": True}
    sign_in_with(env, "google")
    assert env.get("/api/auth/me").json()["user_id"] == existing.user_id
    assert db.execute("SELECT COUNT(*) FROM users WHERE email = 'linked@example.com'").fetchone()[0] == 1
    # The password still works: linking added a way in, it removed none.
    env.post("/api/auth/logout")
    assert env.post(
        "/api/auth/login", json={"email": "linked@example.com", "password": GOOD_PASSWORD}
    ).status_code == 200


def test_a_preregistered_unverified_password_is_discarded_when_google_proves_the_address(env, db):
    squatter = make_user(db, "victim@example.com", verified=False, password="attacker-password")
    env.provider.google_profile = {"sub": "g-v", "email": "victim@example.com", "email_verified": True}
    sign_in_with(env, "google")
    assert env.get("/api/auth/me").json()["user_id"] == squatter.user_id

    user = repo.get_user(db, squatter.user_id)
    assert user.email_verified
    assert not verify_password("attacker-password", user.password_hash)
    env.post("/api/auth/logout")
    refused = env.post(
        "/api/auth/login", json={"email": "victim@example.com", "password": "attacker-password"}
    )
    assert refused.status_code == 401


def test_an_unverified_provider_address_is_never_linked(env, db):
    existing = make_user(db, "owner@example.com")
    env.provider.google_profile = {"sub": "g-x", "email": "owner@example.com", "email_verified": False}
    response = sign_in_with(env, "google")
    assert response.headers["location"] == f"{FRONTEND}/sign-in?auth=verify_email"
    assert env.get("/api/auth/me").status_code == 401
    linked = db.execute("SELECT COUNT(*) FROM user_identities WHERE user_id = ?", (existing.user_id,))
    assert linked.fetchone()[0] == 0


def test_github_uses_the_primary_verified_address(env, db):
    env.provider.github_user = {"id": 42, "login": "octo", "name": None, "email": None}
    env.provider.github_emails = [
        {"email": "old@example.com", "primary": False, "verified": True},
        {"email": "octo@example.com", "primary": True, "verified": True},
        {"email": "octo@users.noreply.github.com", "primary": False, "verified": True},
    ]
    response = sign_in_with(env, "github")
    assert response.headers["location"] == f"{FRONTEND}/sign-in"
    user = repo.get_user_by_email(db, "octo@example.com")
    assert user is not None and user.display_name == "octo"
    assert env.get("/api/auth/me").json()["user_id"] == user.user_id


def test_github_without_a_verified_address_proves_one_by_code(env, db):
    env.provider.github_user = {"id": 7, "login": "noemail", "name": "No Email", "email": None}
    env.provider.github_emails = [
        {"email": "unverified@example.com", "primary": True, "verified": False},
    ]
    response = sign_in_with(env, "github")
    assert response.headers["location"] == f"{FRONTEND}/sign-in?auth=verify_email"
    # No account was created from an address nobody has proven.
    assert repo.get_user_by_email(db, "unverified@example.com") is None
    assert env.get("/api/auth/me").status_code == 401

    status = env.get("/api/auth/verification").json()
    assert status["pending"] is True
    assert status["verification"]["needs_email"] is True
    assert status["verification"]["provider"] == "github"

    chosen = env.post("/api/auth/verification/email", json={"email": "Chosen@Example.com"})
    assert chosen.status_code == 200
    assert chosen.json()["verification"]["email_hint"] == "c••••@example.com"
    code = env.mail.codes_for("chosen@example.com")[0]
    assert verify(env, code).status_code == 200

    user = repo.get_user_by_email(db, "chosen@example.com")
    assert user is not None and user.email_verified and user.display_name == "No Email"
    assert env.get("/api/auth/me").json()["user_id"] == user.user_id
    # And the next GitHub sign-in goes straight in.
    env.post("/api/auth/logout")
    assert sign_in_with(env, "github").headers["location"] == f"{FRONTEND}/sign-in"
    assert env.get("/api/auth/me").json()["user_id"] == user.user_id


def test_github_fallback_links_to_an_existing_account_only_after_the_code(env, db):
    existing = make_user(db, "mine@example.com")
    env.provider.github_user = {"id": 8, "login": "claimant"}
    env.provider.github_emails = []
    sign_in_with(env, "github")
    env.post("/api/auth/verification/email", json={"email": "mine@example.com"})
    # A wrong code does not link anything.
    code = env.mail.codes_for("mine@example.com")[0]
    verify(env, wrong_code(code))
    assert db.execute("SELECT COUNT(*) FROM user_identities").fetchone()[0] == 0
    assert verify(env, code).status_code == 200
    assert env.get("/api/auth/me").json()["user_id"] == existing.user_id


def test_a_callback_without_the_state_cookie_is_refused(env, full_db):
    _, query = start(env, "google")
    env.cookies.clear()
    env.provider.google_profile = {"sub": "g-csrf", "email": "csrf@example.com", "email_verified": True}
    response = callback(env, "google", code="c", state=query["state"][0])
    assert response.headers["location"] == f"{FRONTEND}/sign-in?auth_error=oauth_state"
    assert env.get("/api/auth/me").status_code == 401


def test_a_forged_state_is_refused(env):
    start(env, "google")
    response = callback(env, "google", code="c", state="not-the-state")
    assert response.headers["location"].endswith("auth_error=oauth_state")


def test_a_state_cannot_be_replayed(env):
    env.provider.google_profile = {"sub": "g-r", "email": "replay@example.com", "email_verified": True}
    _, query = start(env, "google")
    state = query["state"][0]
    cookie = env.cookies.get("astrion_session_oauth_state")
    assert callback(env, "google", code="c", state=state).headers["location"] == f"{FRONTEND}/sign-in"
    env.cookies.set("astrion_session_oauth_state", cookie, path="/api/auth/oauth")
    replay = callback(env, "google", code="c", state=state)
    assert replay.headers["location"].endswith("auth_error=oauth_state")


def test_a_state_for_one_provider_does_not_open_another(env):
    _, query = start(env, "google")
    cookie = env.cookies.get("astrion_session_oauth_state")
    env.cookies.set("astrion_session_oauth_state", cookie, path="/api/auth/oauth")
    response = callback(env, "github", code="c", state=query["state"][0])
    assert response.headers["location"].endswith("auth_error=oauth_state")


def test_cancelling_at_the_provider_returns_to_sign_in(env):
    _, query = start(env, "github")
    response = callback(env, "github", error="access_denied", state=query["state"][0])
    assert response.headers["location"] == f"{FRONTEND}/sign-in?auth_error=oauth_cancelled"
    assert env.get("/api/auth/me").status_code == 401


def test_a_provider_error_returns_to_sign_in(env):
    env.provider.token_status = 400
    response = sign_in_with(env, "google")
    assert response.headers["location"] == f"{FRONTEND}/sign-in?auth_error=oauth_failed"
    assert env.get("/api/auth/me").status_code == 401


def test_the_callback_redirect_is_not_user_controlled(env):
    env.provider.google_profile = {"sub": "g-o", "email": "open@example.com", "email_verified": True}
    _, query = start(env, "google")
    response = callback(
        env, "google", code="c", state=query["state"][0],
        next="https://evil.example.net/", redirect_uri="https://evil.example.net/",
    )
    assert response.headers["location"] == f"{FRONTEND}/sign-in"


def test_provider_sign_in_is_one_factor_when_mfa_is_on(env, db):
    user = make_user(db, "twofactor@example.com")
    repo.set_mfa_secret(db, user.user_id, totp.new_secret())
    repo.set_mfa_enabled(db, user.user_id, True)
    env.provider.google_profile = {"sub": "g-m", "email": "twofactor@example.com", "email_verified": True}
    sign_in_with(env, "google")
    me = env.get("/api/auth/me")
    assert me.status_code == 401
    assert me.json()["error"]["details"]["mfa_required"] is True


def test_a_disabled_account_cannot_be_opened_by_a_provider(env, db):
    user = make_user(db, "gone@example.com")
    with db:
        db.execute("UPDATE users SET status = 'disabled' WHERE user_id = ?", (user.user_id,))
    env.provider.google_profile = {"sub": "g-d", "email": "gone@example.com", "email_verified": True}
    response = sign_in_with(env, "google")
    assert response.headers["location"].endswith("auth_error=oauth_account")
    assert env.get("/api/auth/me").status_code == 401


def _application_log(caplog) -> str:
    # `httpx` is the *test client's* own request log, not the application's.
    return "\n".join(r.getMessage() for r in caplog.records if r.name != "httpx")


def test_nothing_the_provider_sends_back_is_logged(env, caplog):
    env.provider.token_status = 400
    with caplog.at_level("DEBUG"):
        sign_in_with(env, "google", code="very-secret-authorization-code")
    assert "very-secret-authorization-code" not in _application_log(caplog)
    assert "google-client-secret-value" not in _application_log(caplog)
    assert "provider-access-token-secret" not in _application_log(caplog)


def test_the_access_log_drops_the_callback_query():
    """uvicorn's request line would otherwise record the authorization code."""
    import logging

    from app.backend.api.app import RedactOAuthQuery

    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/api/auth/oauth/google/callback?code=abc&state=xyz", "1.1", 302),
        None,
    )
    RedactOAuthQuery().filter(record)
    assert "code=abc" not in record.getMessage()
    assert "/api/auth/oauth/google/callback?[redacted]" in record.getMessage()


def test_codes_are_not_logged(env, caplog):
    with caplog.at_level("DEBUG"):
        register(env, "logs@example.com")
        code = env.mail.codes_for("logs@example.com")[0]
        verify(env, wrong_code(code))
        verify(env, code)
    assert code not in _application_log(caplog)
