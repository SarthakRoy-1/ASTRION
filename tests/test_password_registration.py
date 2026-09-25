"""Email + password accounts on a deployment that cannot email a code.

`REQUIRE_VERIFIED_EMAIL=false` is for a deployment with no way to deliver
mail. Registration then creates an account and signs nobody in; the caller
signs in with the password it chose, through the same login every later visit
uses. What these tests hold on to:

- the password is only ever stored as a hash, and login checks it against that
  hash — a wrong password and an unknown address are refused identically;
- nothing about the workspace is reachable without a session that login issued;
- the response cannot be used to learn whether an address is registered;
- none of the verification, code or provider machinery is switched off — the
  default configuration is untouched, and turning the requirement back on picks
  an account made this way up at its next sign-in.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.backend.api.app import create_app
from app.backend.core.config import AuthMode, Settings
from app.backend.services.database import get_connection, initialize_schema

PASSWORD = "correct-horse-battery-staple"
GENERIC_REFUSAL = "Incorrect email address or password."


class CapturingEmailProvider:
    """Records mail; a deployment with no mail transport must send none."""

    def __init__(self) -> None:
        self.codes: list[tuple[str, str]] = []
        self.notices: list[str] = []

    def send_verification_email(self, *, to_address, display_name, verification_url):
        raise AssertionError("no verification links are sent any more")

    def send_verification_code(self, *, to_address, display_name, code, expires_minutes):
        self.codes.append((to_address, code))

    def send_existing_account_notice(self, *, to_address):
        self.notices.append(to_address)


def _settings(full_db, **overrides) -> Settings:
    base = dict(
        database_path=full_db,
        cors_allow_origins=(),
        auth_mode=AuthMode.SESSION,
        # The test transport is http, and a Secure cookie would not be returned.
        session_cookie_secure=False,
        rate_limit_enabled=False,
    )
    base.update(overrides)
    return Settings(**base)


def _client(settings: Settings) -> TestClient:
    app = create_app(settings)
    app.state.email_provider = CapturingEmailProvider()
    return TestClient(app)


@pytest.fixture
def client(full_db):
    """The deployment this change is for: no address proof required."""
    with _client(_settings(full_db, require_verified_email=False)) as c:
        yield c


@pytest.fixture
def db(full_db):
    conn = get_connection(full_db)
    initialize_schema(conn)
    yield conn
    conn.close()


def register(c: TestClient, email: str, password: str = PASSWORD, name: str = "New Person"):
    return c.post(
        "/api/auth/register",
        json={"email": email, "password": password, "display_name": name},
    )


def sign_in(c: TestClient, email: str, password: str = PASSWORD):
    return c.post("/api/auth/login", json={"email": email, "password": password})


# --- registration without a verification ----------------------------------------


def test_registration_needs_no_verification_and_starts_no_session(client):
    response = register(client, "new@example.com")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "registered"
    assert body["verification"] is None
    assert body["email_sent"] is False
    # No verification cookie, no session cookie, and nothing emailed.
    assert not client.cookies
    assert client.app.state.email_provider.codes == []
    assert client.get("/api/auth/me").status_code == 401


def test_the_registration_response_carries_no_credential_or_hash(client):
    text = register(client, "clean@example.com").text
    assert PASSWORD not in text
    assert "scrypt" not in text
    assert "password_hash" not in text


# --- password storage ---------------------------------------------------------


def test_the_password_is_stored_only_as_a_hash(client, db):
    register(client, "hashed@example.com")

    row = db.execute(
        "SELECT * FROM users WHERE email = ?", ("hashed@example.com",)
    ).fetchone()
    stored = row["password_hash"]
    assert stored.startswith("scrypt$")
    assert PASSWORD not in stored
    # The plaintext is in no column of the row at all.
    assert all(PASSWORD not in str(value) for value in tuple(row))
    # It is a salted hash: the same password registered again differs.
    register(client, "hashed-too@example.com")
    other = db.execute(
        "SELECT password_hash FROM users WHERE email = ?", ("hashed-too@example.com",)
    ).fetchone()["password_hash"]
    assert other != stored


def test_an_account_made_this_way_is_recorded_as_unverified(client, db):
    register(client, "unproven@example.com")
    row = db.execute(
        "SELECT email_verified FROM users WHERE email = ?", ("unproven@example.com",)
    ).fetchone()
    assert row["email_verified"] == 0


# --- signing in ---------------------------------------------------------------


def test_the_new_user_signs_in_with_the_password_they_chose(client):
    register(client, "member@example.com")

    response = sign_in(client, "member@example.com")

    assert response.status_code == 200
    assert response.json()["status"] == "authenticated"
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user_id"] == response.json()["user_id"]
    assert me.json()["display_name"] == "New Person"
    # Nothing the browser is sent contains the hash.
    assert "password_hash" not in me.text
    assert "scrypt" not in me.text


def test_a_wrong_password_is_refused(client):
    register(client, "guarded@example.com")

    response = sign_in(client, "guarded@example.com", "not-the-right-password")

    assert response.status_code == 401
    assert response.json()["error"]["message"] == GENERIC_REFUSAL
    assert client.get("/api/auth/me").status_code == 401


def test_an_unknown_address_is_refused_identically(client):
    register(client, "known@example.com")

    unknown = sign_in(client, "nobody@example.com")
    wrong = sign_in(client, "known@example.com", "not-the-right-password")

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["error"]["message"] == wrong.json()["error"]["message"] == GENERIC_REFUSAL
    assert client.get("/api/auth/me").status_code == 401


def test_registering_does_not_admit_anyone_without_the_password(client):
    """Registering an address is not signing in as it."""
    register(client, "owner@example.com")
    attacker = register(client, "owner@example.com", "a-different-password-entirely")

    assert attacker.status_code == 200
    assert sign_in(client, "owner@example.com", "a-different-password-entirely").status_code == 401
    assert sign_in(client, "owner@example.com").status_code == 200


def test_a_taken_and_a_free_address_are_answered_alike(client):
    register(client, "taken@example.com")

    taken = register(client, "taken@example.com", "another-long-password").json()
    free = register(client, "free@example.com").json()

    assert taken == free


def test_a_password_that_fails_validation_is_still_refused(client):
    short = register(client, "short@example.com", "short")

    assert short.status_code == 400
    assert "at least" in short.json()["error"]["message"]
    assert sign_in(client, "short@example.com", "short").status_code == 401


# --- the workspace stays behind a session -----------------------------------------


def test_an_authenticated_user_reaches_the_workspace(client):
    register(client, "worker@example.com")
    assert sign_in(client, "worker@example.com").status_code == 200

    created = client.post("/api/workspaces", json={"name": "Assessment Ops", "workspace_password": "shared-workspace-pw", "confirm_workspace_password": "shared-workspace-pw"})
    assert created.status_code == 201
    listing = client.get("/api/workspaces")
    assert listing.status_code == 200
    assert "Assessment Ops" in listing.text


def test_an_unauthenticated_caller_reaches_no_workspace_route(client):
    register(client, "bystander@example.com")  # an account, but no session

    for method, path, body in (
        ("get", "/api/workspaces", None),
        ("post", "/api/workspaces", {"name": "Sneaky", "workspace_password": "shared-workspace-pw", "confirm_workspace_password": "shared-workspace-pw"}),
        ("get", "/api/auth/me", None),
        ("post", "/api/chat", {"message": "hello"}),
        ("get", "/api/actions/pending", None),
    ):
        response = getattr(client, method)(path, **({"json": body} if body else {}))
        assert response.status_code == 401, path


def test_a_forged_session_is_refused(client):
    client.cookies.set("astrion_session", "not-a-real-token")
    assert client.get("/api/workspaces").status_code == 401


# --- nothing that verification needs was removed ---------------------------------


def test_the_default_configuration_still_starts_the_code_flow(full_db):
    with _client(_settings(full_db)) as default:
        response = register(default, "coded@example.com")

        body = response.json()
        assert body["status"] == "registration_received"
        assert body["verification"]["email_hint"]
        assert default.get("/api/auth/me").status_code == 401
        assert len(default.app.state.email_provider.codes) == 1


def test_the_verification_and_provider_routes_are_still_served(client):
    paths = client.get("/openapi.json").json()["paths"]
    for expected in (
        "/api/auth/verification",
        "/api/auth/verification/verify",
        "/api/auth/verification/resend",
        "/api/auth/oauth/{provider}/start",
        "/api/auth/oauth/{provider}/callback",
    ):
        assert expected in paths


def test_turning_verification_back_on_picks_an_existing_account_up(full_db):
    with _client(_settings(full_db, require_verified_email=False)) as before:
        register(before, "later@example.com")

    with _client(_settings(full_db)) as after:
        refused = sign_in(after, "later@example.com")

        # The right password on an unproven address is routed to the code flow
        # rather than admitted, exactly as an account made before this change.
        assert refused.status_code == 401
        assert refused.json()["error"]["code"] == "email_verification_required"
        assert after.get("/api/auth/me").status_code == 401
        code = after.app.state.email_provider.codes[-1][1]
        verified = after.post("/api/auth/verification/verify", json={"code": code})
        assert verified.status_code == 200
        assert after.get("/api/auth/me").status_code == 200


def test_health_lists_no_provider_when_none_is_configured(client):
    health = client.get("/health").json()
    assert health["oauth_providers"] == []
