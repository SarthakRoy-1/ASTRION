"""Phase 6: the seeded public-demo tenant, and what it must not become.

A public demo is a standing invitation to whoever finds the URL, so the thing
under test is not really "does the seed work" — it is that the way in is an
*ordinary* way in. The demo accounts sign in at the same endpoint, carry the
same permission sets their roles have always carried, and meet the same
confirmation gate and manager threshold as anyone else. Nothing here may pass
if a demo-shaped shortcut exists.

Everything goes over HTTP under real sessions, because a demo visitor is a
real caller and the questions are about the boundary they meet.
"""

from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from app.backend.auth import repository as repo
from app.backend.auth import workspaces as workspace_service
from app.backend.auth.passwords import hash_password
from app.backend.auth.permissions import OrgRole, Permission, permissions_for
from app.backend.auth.workspaces import slugify
from app.backend.core.config import AuthMode, Settings
from scripts.seed_demo import (
    DEMO_USERS,
    DEMO_WORKSPACE_NAME,
    DEMO_WORKSPACE_SLUG,
    seed,
)

#: Not the deployed password. The real one is a deployment secret and lives
#: nowhere in this repository — this is a fixture, the way every other suite
#: here uses a fixture password.
DEMO_PASSWORD = "demo-password-for-tests"

SUPPORT = "support@demo.parcelpilot.example"
OPERATIONS = "operations@demo.parcelpilot.example"
OWNER = "owner@demo.parcelpilot.example"

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def settings(full_db):
    return Settings(
        database_path=full_db,
        cors_allow_origins=(),
        auth_mode=AuthMode.SESSION,
        session_cookie_secure=False,
        rate_limit_enabled=False,
    )


@pytest.fixture
def db(full_db):
    from app.backend.services.database import get_connection, initialize_schema

    conn = get_connection(full_db)
    initialize_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def seeded(db):
    """The demo tenant, seeded once, as a deployment's first boot would."""
    return seed(db, password=DEMO_PASSWORD)


def client_for(settings, email: str, password: str = DEMO_PASSWORD) -> TestClient:
    from app.backend.api.app import create_app

    client = TestClient(create_app(settings))
    response = client.post(
        "/api/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    return client


def demo_org(db) -> str:
    row = db.execute(
        "SELECT org_id FROM organizations WHERE slug = ?", (DEMO_WORKSPACE_SLUG,)
    ).fetchone()
    assert row is not None
    return row["org_id"]


# ===========================================================================
# What the seed creates
# ===========================================================================


def test_the_seed_creates_the_demo_workspace(db, seeded):
    assert seeded["workspace_created"] is True
    assert seeded["slug"] == DEMO_WORKSPACE_SLUG
    row = db.execute(
        "SELECT name, status FROM organizations WHERE slug = ?",
        (DEMO_WORKSPACE_SLUG,),
    ).fetchone()
    assert row["name"] == DEMO_WORKSPACE_NAME
    assert row["status"] == "active"


def test_the_workspace_name_still_produces_the_slug_the_seed_looks_for(db):
    """The idempotency key is derived, so renaming must not silently fork it.

    `create_workspace` suffixes a colliding slug rather than refusing. If the
    name stopped slugifying to the constant, the first boot would make
    `parcelpilot-demo` and every later one would look for a slug it never
    created — a new tenant per restart.
    """
    assert slugify(DEMO_WORKSPACE_NAME) == DEMO_WORKSPACE_SLUG


def test_the_seed_creates_exactly_the_expected_users(db, seeded):
    assert sorted(seeded["users_created"]) == sorted([SUPPORT, OPERATIONS, OWNER])
    for email, _display, _role in DEMO_USERS:
        assert repo.get_user_by_email(db, email) is not None


def test_the_seeded_users_are_verified(db, seeded):
    """Otherwise they could not sign in, and there is no mail transport."""
    for email, _display, _role in DEMO_USERS:
        user = repo.get_user_by_email(db, email)
        assert user.email_verified is True


def test_the_seeded_roles_are_the_ones_the_matrix_defines(db, seeded):
    org = demo_org(db)
    expected = {email: role for email, _display, role in DEMO_USERS}
    for email, role in expected.items():
        user = repo.get_user_by_email(db, email)
        membership = repo.get_membership(db, org_id=org, user_id=user.user_id)
        assert membership is not None
        assert membership.role is role


def test_the_dataset_accounts_are_attached(db, seeded):
    org = demo_org(db)
    attached = {
        row["account_id"]
        for row in db.execute(
            "SELECT account_id FROM organization_accounts WHERE org_id = ?", (org,)
        )
    }
    ingested = {row["account_id"] for row in db.execute("SELECT account_id FROM accounts")}
    assert attached == ingested
    assert attached, "the fixture database should hold dataset accounts"


def test_the_seed_grants_no_permission_a_role_does_not_already_carry(db, seeded):
    """There is no demo-only capability. The matrix is still the only source."""
    org = demo_org(db)
    for email, _display, role in DEMO_USERS:
        user = repo.get_user_by_email(db, email)
        membership = repo.get_membership(db, org_id=org, user_id=user.user_id)
        assert permissions_for(membership.role) == permissions_for(role)


# ===========================================================================
# Idempotency
# ===========================================================================


def test_running_the_seed_twice_changes_nothing(db, seeded):
    again = seed(db, password=DEMO_PASSWORD)

    assert again["workspace_created"] is False
    assert again["users_created"] == []
    assert again["memberships_created"] == []
    assert again["accounts_attached"] == []
    assert again["org_id"] == seeded["org_id"]


def test_repeated_seeding_creates_no_duplicate_workspace(db, seeded):
    for _ in range(3):
        seed(db, password=DEMO_PASSWORD)
    count = db.execute(
        "SELECT COUNT(*) FROM organizations WHERE slug LIKE ?",
        (f"{DEMO_WORKSPACE_SLUG}%",),
    ).fetchone()[0]
    assert count == 1


def test_repeated_seeding_creates_no_duplicate_users(db, seeded):
    for _ in range(3):
        seed(db, password=DEMO_PASSWORD)
    for email, _display, _role in DEMO_USERS:
        count = db.execute(
            "SELECT COUNT(*) FROM users WHERE email = ?", (email,)
        ).fetchone()[0]
        assert count == 1


def test_repeated_seeding_creates_no_duplicate_membership(db, seeded):
    org = demo_org(db)
    for _ in range(3):
        seed(db, password=DEMO_PASSWORD)
    rows = db.execute(
        "SELECT user_id, COUNT(*) AS n FROM memberships WHERE org_id = ? "
        "GROUP BY user_id",
        (org,),
    ).fetchall()
    assert rows
    assert all(row["n"] == 1 for row in rows)


def test_repeated_seeding_creates_no_duplicate_account_attachment(db, seeded):
    for _ in range(3):
        seed(db, password=DEMO_PASSWORD)
    rows = db.execute(
        "SELECT account_id, COUNT(*) AS n FROM organization_accounts "
        "GROUP BY account_id"
    ).fetchall()
    assert rows
    assert all(row["n"] == 1 for row in rows)


def test_the_seed_never_changes_an_existing_account(db):
    """An address that already exists keeps its password and its state.

    The seed cannot tell an address it created from one somebody registered,
    so it declines to touch either.
    """
    existing = repo.create_user(
        db,
        email=SUPPORT,
        display_name="Someone else",
        password_hash=hash_password("a-different-password"),
        email_verified=False,
    )
    seed(db, password=DEMO_PASSWORD)

    after = repo.get_user_by_email(db, SUPPORT)
    assert after.user_id == existing.user_id
    assert after.display_name == "Someone else"
    assert after.password_hash == existing.password_hash
    assert after.email_verified is False


def test_an_account_owned_by_another_workspace_is_reported_not_moved(db):
    """The unique index is the tenant boundary; a boot script may not redraw it."""
    owner = repo.create_user(
        db,
        email="real@acme.test",
        display_name="Real",
        password_hash=hash_password(DEMO_PASSWORD),
        email_verified=True,
    )
    theirs = workspace_service.create_workspace(
        db, owner_user_id=owner.user_id, name="Acme", account_ids=["ACCT-001"]
    )["org_id"]

    summary = seed(db, password=DEMO_PASSWORD)

    assert ("ACCT-001", theirs) in summary["accounts_skipped"]
    assert "ACCT-001" not in summary["accounts_attached"]
    assert repo.org_owning_account(db, "ACCT-001") == theirs


# ===========================================================================
# Not seeded unless asked for
# ===========================================================================


def test_an_unseeded_database_has_no_demo_tenant(db):
    """Ingestion alone creates nothing. The demo is opt-in, always."""
    assert (
        db.execute(
            "SELECT COUNT(*) FROM organizations WHERE slug = ?",
            (DEMO_WORKSPACE_SLUG,),
        ).fetchone()[0]
        == 0
    )
    for email, _display, _role in DEMO_USERS:
        assert repo.get_user_by_email(db, email) is None


def test_the_container_seeds_only_when_explicitly_enabled():
    """The flag lives in the entrypoint, not in the application.

    Deliberate: a running server with no concept of a demo has nowhere for a
    demo bypass to grow. What has to be asserted, then, is the boot script's
    contract — that the seed is guarded, that the guard defaults to off, and
    that it is not implied by APP_ENV.
    """
    script = (REPO_ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")

    assert "scripts/seed_demo.py" in script
    assert '[ "${DEMO_SEED_ENABLED:-false}" = "true" ]' in script

    guard = script.index('DEMO_SEED_ENABLED:-false')
    call = script.index("scripts/seed_demo.py")
    assert guard < call, "the seed must be inside its guard"

    # The flag is its own decision, never inherited from calling itself
    # production.
    seed_block = script[guard:]
    assert "APP_ENV" not in seed_block


def test_the_repository_assigns_no_demo_password():
    """The published credential is deployment configuration, never a file here.

    Every tracked mention of the password variables must be a reference, a
    comment, or the documented placeholder — never an assignment carrying a
    value somebody could sign in with.
    """
    import re

    placeholder = "replace-me-in-the-deployment"
    assignment = re.compile(
        r"^\s*(?:#\s*)?(DEMO_SEED_PASSWORD|NEXT_PUBLIC_DEMO_PASSWORD)=(.*)$"
    )

    for path in sorted(REPO_ROOT.glob("**/*")):
        if not path.is_file() or path.suffix in {".pyc"}:
            continue
        parts = set(path.parts)
        if parts & {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line in text.splitlines():
            match = assignment.match(line)
            if match is None:
                continue
            value = match.group(2).strip().strip("\"'")
            assert value in {"", placeholder}, (
                f"{path.relative_to(REPO_ROOT)} assigns "
                f"{match.group(1)}={value!r}"
            )


# ===========================================================================
# The way in is an ordinary way in
# ===========================================================================


def test_a_demo_user_signs_in_through_the_ordinary_endpoint(settings, seeded):
    """No demo login route, no minted session, no header. The same POST."""
    from app.backend.api.app import create_app

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/auth/login", json={"email": SUPPORT, "password": DEMO_PASSWORD}
        )
        assert response.status_code == 200
        assert client.cookies.get("parcelpilot_session")
        assert client.get("/api/auth/me").status_code == 200


def test_the_wrong_password_is_refused_for_a_demo_account_too(settings, seeded):
    from app.backend.api.app import create_app

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/auth/login", json={"email": SUPPORT, "password": "not-it"}
        )
    assert response.status_code == 401


def test_there_is_no_endpoint_that_mints_a_session_for_a_named_user(settings, seeded):
    """The obvious shortcut must not exist, under any of its obvious names."""
    from app.backend.api.app import create_app

    app = create_app(settings)
    published = set(app.openapi()["paths"])
    with TestClient(app) as client:
        for shortcut in (
            "/api/demo/session",
            "/api/demo/login",
            "/api/auth/demo",
            "/api/auth/impersonate",
        ):
            assert shortcut not in published
            # Not merely undocumented: unroutable.
            assert client.post(shortcut, json={"user_id": "owner"}).status_code == 404


def test_the_identity_header_cannot_impersonate_under_session_auth(settings, seeded):
    """`X-ParcelPilot-User` is inert outside demo_header mode."""
    with client_for(settings, SUPPORT) as client:
        response = client.get(
            "/api/auth/me", headers={"X-ParcelPilot-User": "support.manager"}
        )
        assert response.status_code == 200
        assert response.json()["role"] == OrgRole.SUPPORT.value


def test_a_demo_visitor_cannot_choose_their_role(settings, db, seeded):
    """The role comes from the membership row, not from anything sent."""
    with client_for(settings, SUPPORT) as client:
        body = client.get("/api/auth/me").json()
    assert body["role"] == OrgRole.SUPPORT.value
    assert set(body["permissions"]) == {
        p.value for p in permissions_for(OrgRole.SUPPORT)
    }


def test_a_demo_user_cannot_reach_another_workspace(settings, db, seeded):
    owner = repo.create_user(
        db,
        email="stranger@acme.test",
        display_name="Stranger",
        password_hash=hash_password(DEMO_PASSWORD),
        email_verified=True,
    )
    theirs = workspace_service.create_workspace(
        db, owner_user_id=owner.user_id, name="Somebody Else", account_ids=[]
    )["org_id"]

    with client_for(settings, OWNER) as client:
        assert client.get(f"/api/workspaces/{theirs}").status_code == 404
        assert client.get(f"/api/workspaces/{theirs}/members").status_code == 404
        assert client.post(f"/api/workspaces/{theirs}/activate").status_code == 404


# ===========================================================================
# The controls a visitor will actually meet
# ===========================================================================


def test_demo_support_cannot_read_the_audit_trail(settings, seeded):
    with client_for(settings, SUPPORT) as client:
        assert client.get("/api/auth/audit").status_code == 403


@pytest.mark.parametrize("email", [OPERATIONS, OWNER])
def test_demo_operations_and_owner_can_read_the_audit_trail(settings, seeded, email):
    with client_for(settings, email) as client:
        response = client.get("/api/auth/audit")
    assert response.status_code == 200
    assert response.json()["chain_intact"] is True


def test_demo_permissions_are_matrix_driven(settings, seeded):
    for email, _display, role in DEMO_USERS:
        with client_for(settings, email) as client:
            granted = set(client.get("/api/auth/me").json()["permissions"])
        assert granted == {p.value for p in permissions_for(role)}


def test_demo_support_cannot_execute_an_action(settings, seeded):
    """Preparing and executing stay separate for a demo visitor too."""
    with client_for(settings, SUPPORT) as client:
        granted = set(client.get("/api/auth/me").json()["permissions"])
    assert Permission.PROPOSE_ACTION.value in granted
    assert Permission.EXECUTE_ACTION.value not in granted


def test_demo_operations_cannot_approve_a_high_value_action(settings, seeded):
    """The manager threshold is not relaxed for the demo tenant."""
    with client_for(settings, OPERATIONS) as client:
        granted = set(client.get("/api/auth/me").json()["permissions"])
    assert Permission.EXECUTE_ACTION.value in granted
    assert Permission.APPROVE_HIGH_VALUE_ACTION.value not in granted

    with client_for(settings, OWNER) as client:
        owner_granted = set(client.get("/api/auth/me").json()["permissions"])
    assert Permission.APPROVE_HIGH_VALUE_ACTION.value in owner_granted


def test_a_demo_action_still_stops_at_the_confirmation_gate(settings, db, seeded):
    """Prepared, previewed, and not executed until somebody says so."""
    with client_for(settings, OPERATIONS) as client:
        answer = client.post(
            "/api/chat",
            json={
                "message": (
                    "ORD-2002 missed its pickup window. Prepare a service "
                    "credit for it."
                )
            },
        ).json()

    assert answer["action_status"] == "pending_confirmation"
    proposal = answer["proposed_action"]
    assert proposal is not None
    assert proposal["confirmation_required"] is True

    executed = db.execute(
        "SELECT COUNT(*) FROM service_credits WHERE order_id = 'ORD-2002'"
    ).fetchone()[0]
    assert executed == 0


# ===========================================================================
# What the demo does not change
# ===========================================================================


def test_ordinary_registration_still_requires_verification(settings, seeded):
    """Seeding a demo account must not open a door for everyone else."""
    from app.backend.api.app import create_app

    with TestClient(create_app(settings)) as client:
        registered = client.post(
            "/api/auth/register",
            json={
                "email": "newcomer@acme.test",
                "password": "a-long-enough-password",
                "display_name": "Newcomer",
            },
        )
        assert registered.status_code == 200

        refused = client.post(
            "/api/auth/login",
            json={"email": "newcomer@acme.test", "password": "a-long-enough-password"},
        )
    assert refused.status_code == 401


def test_the_seed_does_not_verify_anyone_it_did_not_create(db, seeded):
    unrelated = repo.create_user(
        db,
        email="unrelated@acme.test",
        display_name="Unrelated",
        password_hash=hash_password(DEMO_PASSWORD),
        email_verified=False,
    )
    seed(db, password=DEMO_PASSWORD)
    assert repo.get_user_by_email(db, "unrelated@acme.test").email_verified is False
    assert unrelated.email_verified is False
