"""Creating a workspace with a password, and joining it with a code and that password.

The model under test: the account signs in as before (email + password, an
`astrion_session` cookie); a *workspace* is created with a name and a password
its owner chooses, and the server generates the code people join it with.
Access needs the code **and** the password, and only ever for an account that is
already signed in. What these tests hold on to:

- the password is stored only as a hash, in a table no workspace read touches,
  and appears in no response;
- the code is random, unique at the database, and cannot be chosen;
- a wrong password, an unknown code and a workspace with no join password are
  all answered identically, and guessing is throttled;
- membership stays the only thing that opens a workspace, and joining grants the
  least role;
- changing the password ends the old one and leaves members alone.
"""

from __future__ import annotations

import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.backend.api.app import create_app
from app.backend.auth import repository as repo
from app.backend.auth import workspaces as workspace_service
from app.backend.auth.passwords import hash_password
from app.backend.auth.permissions import OrgRole
from app.backend.core.config import AuthMode, Settings
from app.backend.services.database import get_connection, initialize_schema

ACCOUNT_PASSWORD = "correct-horse-battery-staple"
WORKSPACE_PASSWORD = "team-shared-passphrase"
NEW_PASSWORD = "a-fresh-team-passphrase"
CODE_SHAPE = re.compile(r"^[A-HJ-NP-Z2-9]{10}$")
REFUSAL = workspace_service.JOIN_REFUSAL


# --- fixtures -----------------------------------------------------------------


def _settings(full_db, **overrides) -> Settings:
    base = dict(
        database_path=full_db,
        cors_allow_origins=(),
        auth_mode=AuthMode.SESSION,
        session_cookie_secure=False,
        rate_limit_enabled=False,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def app(full_db):
    return create_app(_settings(full_db))


@pytest.fixture
def db(full_db):
    conn = get_connection(full_db)
    initialize_schema(conn)
    yield conn
    conn.close()


def make_user(db, email: str) -> str:
    return repo.create_user(
        db,
        email=email,
        display_name=email.split("@")[0],
        password_hash=hash_password(ACCOUNT_PASSWORD),
        email_verified=True,
    ).user_id


def sign_in(app, db, email: str) -> TestClient:
    """A separate browser for `email`, signed in the ordinary way."""
    if repo.get_user_by_email(db, email) is None:
        make_user(db, email)
    client = TestClient(app)
    response = client.post(
        "/api/auth/login", json={"email": email, "password": ACCOUNT_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return client


def create_body(name: str = "Assessment Ops", password: str = WORKSPACE_PASSWORD) -> dict:
    return {
        "name": name,
        "workspace_password": password,
        "confirm_workspace_password": password,
    }


def create_workspace(client: TestClient, name: str = "Assessment Ops", **kw) -> dict:
    response = client.post("/api/workspaces", json=create_body(name, **kw))
    assert response.status_code == 201, response.text
    return response.json()


def join(client: TestClient, code: str, password: str = WORKSPACE_PASSWORD):
    return client.post(
        "/api/workspaces/join",
        json={"workspace_code": code, "workspace_password": password},
    )


@pytest.fixture
def owner(app, db):
    return sign_in(app, db, "owner@example.com")


@pytest.fixture
def team(owner):
    """The owner's workspace: its view (with the code) as the creator saw it."""
    return create_workspace(owner)


# --- creating a workspace ---------------------------------------------------------


def test_a_signed_in_user_can_create_a_workspace(owner):
    response = owner.post("/api/workspaces", json=create_body())

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Assessment Ops"
    assert body["workspace_id"].startswith("ORG-")
    assert body["role"] == "owner"


def test_the_code_is_generated_by_the_server(team, owner):
    assert CODE_SHAPE.match(team["workspace_code"])

    # There is no field to choose one with.
    response = owner.post(
        "/api/workspaces", json={**create_body("Mine"), "workspace_code": "CHOSEN2345"}
    )
    assert response.status_code == 422


def test_codes_are_unique_and_unpredictable(owner):
    codes = [create_workspace(owner, f"W{i}")["workspace_code"] for i in range(6)]

    assert len(set(codes)) == 6
    assert all(CODE_SHAPE.match(c) for c in codes)
    # Not a counter, not derived from the name.
    assert codes != sorted(codes)


def test_a_duplicate_code_is_refused_by_the_database(db, team):
    other = repo.create_organization(db, name="Other", slug="other")[0]
    user_id = repo.get_user_by_email(db, "owner@example.com").user_id

    with pytest.raises(sqlite3.IntegrityError):
        repo.create_workspace_access(
            db,
            org_id=other,
            workspace_code=team["workspace_code"],
            password_hash="scrypt$x",
            owner_user_id=user_id,
        )


def test_a_code_collision_is_retried_not_returned(db, monkeypatch, team):
    """The generator returning a taken code must not fail the creation."""
    taken = team["workspace_code"]
    issued = iter([taken, taken, "FRESHCODE22"])
    monkeypatch.setattr(workspace_service, "generate_workspace_code", lambda: next(issued))
    user_id = repo.get_user_by_email(db, "owner@example.com").user_id

    workspace = workspace_service.create_workspace(
        db, owner_user_id=user_id, name="Second", workspace_password=WORKSPACE_PASSWORD
    )

    assert repo.get_workspace_code(db, workspace["org_id"]) == "FRESHCODE22"


def test_the_creator_becomes_the_owner_and_a_member(owner, team, db):
    user = repo.get_user_by_email(db, "owner@example.com")
    membership = repo.get_membership(db, org_id=team["workspace_id"], user_id=user.user_id)

    assert membership is not None and membership.role is OrgRole.OWNER
    listing = owner.get("/api/workspaces").json()
    assert [w["workspace_id"] for w in listing["workspaces"]] == [team["workspace_id"]]
    # And is in it straight away, with no separate switch.
    assert listing["active_workspace_id"] == team["workspace_id"]
    assert owner.get(f"/api/workspaces/{team['workspace_id']}").status_code == 200
    # The recorded owner is the creator.
    row = db.execute(
        "SELECT owner_user_id FROM workspace_access WHERE org_id = ?", (team["workspace_id"],)
    ).fetchone()
    assert row["owner_user_id"] == user.user_id


def test_the_password_is_stored_only_as_a_hash(team, db):
    row = db.execute(
        "SELECT * FROM workspace_access WHERE org_id = ?", (team["workspace_id"],)
    ).fetchone()

    assert row["password_hash"].startswith("scrypt$")
    assert WORKSPACE_PASSWORD not in row["password_hash"]
    assert all(WORKSPACE_PASSWORD not in str(value) for value in tuple(row))
    org = db.execute(
        "SELECT * FROM organizations WHERE org_id = ?", (team["workspace_id"],)
    ).fetchone()
    assert all(WORKSPACE_PASSWORD not in str(value) for value in tuple(org))
    # Salted: the same password on another workspace hashes differently.
    other = db.execute(
        "SELECT password_hash FROM workspace_access WHERE org_id != ?",
        (team["workspace_id"],),
    ).fetchall()
    assert all(o["password_hash"] != row["password_hash"] for o in other)


def test_no_response_carries_the_password_or_its_hash(app, db, owner, team):
    member = sign_in(app, db, "member@example.com")
    responses = [
        owner.get("/api/workspaces"),
        owner.get(f"/api/workspaces/{team['workspace_id']}"),
        owner.get(f"/api/workspaces/{team['workspace_id']}/members"),
        owner.post(f"/api/workspaces/{team['workspace_id']}/activate"),
        owner.patch(f"/api/workspaces/{team['workspace_id']}", json={"name": "Renamed"}),
        join(member, team["workspace_code"]),
        member.get("/api/workspaces"),
        owner.post(
            f"/api/workspaces/{team['workspace_id']}/password",
            json={"new_password": NEW_PASSWORD, "confirm_new_password": NEW_PASSWORD},
        ),
    ]
    for response in responses:
        assert response.status_code == 200, response.text
        for forbidden in (WORKSPACE_PASSWORD, NEW_PASSWORD, "scrypt", "password_hash"):
            assert forbidden not in response.text
    created = owner.post("/api/workspaces", json=create_body("Another"))
    assert WORKSPACE_PASSWORD not in created.text and "scrypt" not in created.text


def test_a_password_that_does_not_match_its_confirmation_is_refused(owner, db):
    body = {**create_body(), "confirm_workspace_password": "something-different"}

    response = owner.post("/api/workspaces", json=body)

    assert response.status_code == 400
    assert "do not match" in response.json()["error"]["message"]
    assert db.execute("SELECT COUNT(*) AS n FROM workspace_access").fetchone()["n"] == 0


def test_a_weak_workspace_password_is_refused(owner, db):
    response = owner.post("/api/workspaces", json=create_body(password="short"))

    assert response.status_code == 400
    assert "at least" in response.json()["error"]["message"]
    assert db.execute("SELECT COUNT(*) AS n FROM workspace_access").fetchone()["n"] == 0
    assert db.execute("SELECT COUNT(*) AS n FROM organizations").fetchone()["n"] == 0


def test_creating_a_workspace_needs_a_session(app):
    assert TestClient(app).post("/api/workspaces", json=create_body()).status_code == 401


# --- joining ---------------------------------------------------------------------


def test_the_right_code_and_password_admit_a_signed_in_user(app, db, team):
    member = sign_in(app, db, "member@example.com")

    response = join(member, team["workspace_code"])

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "joined"
    assert body["workspace_id"] == team["workspace_id"]
    # The least role, and no code: the code is the owner's to hand out.
    assert body["role"] == "viewer"
    assert "workspace_code" not in body
    user = repo.get_user_by_email(db, "member@example.com")
    assert repo.get_membership(db, org_id=team["workspace_id"], user_id=user.user_id) is not None
    assert member.get("/api/workspaces").json()["active_workspace_id"] == team["workspace_id"]


def test_the_code_is_case_and_spacing_insensitive(app, db, team):
    member = sign_in(app, db, "typist@example.com")
    code = team["workspace_code"]
    typed = f"  {code[:5].lower()} - {code[5:].lower()} "

    assert join(member, typed).status_code == 200


def test_a_wrong_workspace_password_is_refused(app, db, team):
    member = sign_in(app, db, "guesser@example.com")

    response = join(member, team["workspace_code"], "not-the-team-password")

    assert response.status_code == 400
    assert response.json()["error"]["message"] == REFUSAL
    user = repo.get_user_by_email(db, "guesser@example.com")
    assert repo.get_membership(db, org_id=team["workspace_id"], user_id=user.user_id) is None


def test_an_unknown_code_is_refused_in_the_same_words(app, db, team):
    member = sign_in(app, db, "lost@example.com")

    unknown = join(member, "ZZZZZZZZZZ")
    wrong = join(member, team["workspace_code"], "not-the-team-password")

    assert unknown.status_code == wrong.status_code == 400
    assert unknown.json()["error"]["message"] == wrong.json()["error"]["message"] == REFUSAL
    assert unknown.json()["error"]["code"] == wrong.json()["error"]["code"]


def test_the_code_alone_grants_nothing(app, db, team):
    member = sign_in(app, db, "codeonly@example.com")

    response = member.post(
        "/api/workspaces/join", json={"workspace_code": team["workspace_code"]}
    )

    assert response.status_code == 422
    assert member.get(f"/api/workspaces/{team['workspace_id']}").status_code == 404


def test_an_unauthenticated_caller_cannot_join(app, team, db):
    response = join(TestClient(app), team["workspace_code"])

    assert response.status_code == 401
    assert db.execute("SELECT COUNT(*) AS n FROM memberships").fetchone()["n"] == 1


def test_a_workspace_with_no_join_password_cannot_be_joined(app, db):
    """Made by a script or the demo seed: it has neither a code nor a password."""
    owner_id = make_user(db, "scripted@example.com")
    scripted = workspace_service.create_workspace(db, owner_user_id=owner_id, name="Scripted")
    member = sign_in(app, db, "member@example.com")

    assert repo.get_workspace_code(db, scripted["org_id"]) is None
    assert join(member, scripted["org_id"]).status_code == 400
    assert join(member, "", WORKSPACE_PASSWORD).status_code == 422


def test_repeated_wrong_guesses_are_throttled_even_for_the_right_answer(app, db, team):
    member = sign_in(app, db, "brute@example.com")
    for _ in range(workspace_service.MAX_JOIN_FAILURES_PER_USER):
        assert join(member, team["workspace_code"], "wrong-guess-here").status_code == 400

    locked = join(member, team["workspace_code"])

    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "rate_limited"
    user = repo.get_user_by_email(db, "brute@example.com")
    assert repo.get_membership(db, org_id=team["workspace_id"], user_id=user.user_id) is None
    # Someone else is not locked out by it.
    other = sign_in(app, db, "bystander@example.com")
    assert join(other, team["workspace_code"]).status_code == 200


def test_joining_again_is_harmless_and_does_not_change_the_role(app, db, owner, team):
    member = sign_in(app, db, "again@example.com")
    join(member, team["workspace_code"])
    user = repo.get_user_by_email(db, "again@example.com")
    owner.patch(
        f"/api/workspaces/{team['workspace_id']}/members/{user.user_id}",
        json={"role": "operations"},
    )

    again = join(member, team["workspace_code"])

    assert again.status_code == 200
    assert again.json()["role"] == "operations"


# --- membership is what opens a workspace -------------------------------------------


def test_a_signed_in_non_member_reaches_nothing_in_the_workspace(app, db, team):
    stranger = sign_in(app, db, "stranger@example.com")
    wid = team["workspace_id"]

    for method, path, body in (
        ("get", f"/api/workspaces/{wid}", None),
        ("get", f"/api/workspaces/{wid}/members", None),
        ("get", f"/api/workspaces/{wid}/invitations", None),
        ("post", f"/api/workspaces/{wid}/activate", None),
        ("patch", f"/api/workspaces/{wid}", {"name": "Mine now"}),
        ("post", f"/api/workspaces/{wid}/password",
         {"new_password": NEW_PASSWORD, "confirm_new_password": NEW_PASSWORD}),
    ):
        response = getattr(stranger, method)(path, **({"json": body} if body else {}))
        # Not "forbidden": a non-member cannot tell this workspace from a missing one.
        assert response.status_code == 404, (method, path)
    listing = stranger.get("/api/workspaces").json()
    assert listing["workspaces"] == [] and listing["needs_workspace"] is True


def test_after_joining_the_user_reaches_what_a_viewer_may(app, db, team):
    member = sign_in(app, db, "member@example.com")
    join(member, team["workspace_code"])
    wid = team["workspace_id"]

    assert member.get(f"/api/workspaces/{wid}").status_code == 200
    assert member.get(f"/api/workspaces/{wid}/members").status_code == 200
    assert member.post(f"/api/workspaces/{wid}/activate").status_code == 200
    # ...and no more than that.
    assert member.patch(f"/api/workspaces/{wid}", json={"name": "Mine"}).status_code == 403
    assert member.get(f"/api/workspaces/{wid}/invitations").status_code == 403
    assert member.post(
        f"/api/workspaces/{wid}/password",
        json={"new_password": NEW_PASSWORD, "confirm_new_password": NEW_PASSWORD},
    ).status_code == 403


def test_knowing_another_workspaces_id_is_not_access_to_it(app, db, owner, team):
    member = sign_in(app, db, "member@example.com")
    join(member, team["workspace_code"])
    other = create_workspace(sign_in(app, db, "someone@example.com"), "Not Yours")
    other_id = other["workspace_id"]

    for method, path, body in (
        ("get", f"/api/workspaces/{other_id}", None),
        ("get", f"/api/workspaces/{other_id}/members", None),
        ("post", f"/api/workspaces/{other_id}/activate", None),
        ("patch", f"/api/workspaces/{other_id}", {"name": "x"}),
    ):
        response = getattr(member, method)(path, **({"json": body} if body else {}))
        assert response.status_code == 404, (method, path)
    # The workspace they were admitted to is still theirs, and only that one.
    ids = [w["workspace_id"] for w in member.get("/api/workspaces").json()["workspaces"]]
    assert ids == [team["workspace_id"]]


# --- the owner's controls --------------------------------------------------------------


def _change(client, workspace_id, password=NEW_PASSWORD, confirm=None):
    return client.post(
        f"/api/workspaces/{workspace_id}/password",
        json={"new_password": password, "confirm_new_password": confirm or password},
    )


def test_the_owner_can_change_the_workspace_password(owner, team):
    response = _change(owner, team["workspace_id"])

    assert response.status_code == 200
    assert response.json()["status"] == "password_changed"
    # The code is unchanged; only the password is new.
    assert response.json()["workspace_code"] == team["workspace_code"]


def test_the_old_password_stops_working_and_the_new_one_works(app, db, owner, team):
    _change(owner, team["workspace_id"])

    before = join(sign_in(app, db, "late@example.com"), team["workspace_code"])
    after = join(sign_in(app, db, "later@example.com"), team["workspace_code"], NEW_PASSWORD)

    assert before.status_code == 400 and before.json()["error"]["message"] == REFUSAL
    assert after.status_code == 200


def test_existing_members_stay_members_after_a_password_change(app, db, owner, team):
    member = sign_in(app, db, "member@example.com")
    join(member, team["workspace_code"])

    _change(owner, team["workspace_id"])

    assert member.get(f"/api/workspaces/{team['workspace_id']}").status_code == 200
    assert member.get(f"/api/workspaces/{team['workspace_id']}/members").status_code == 200
    user = repo.get_user_by_email(db, "member@example.com")
    assert repo.get_membership(db, org_id=team["workspace_id"], user_id=user.user_id) is not None


def test_the_stored_hash_is_replaced(db, owner, team):
    def stored() -> str:
        return db.execute(
            "SELECT password_hash FROM workspace_access WHERE org_id = ?",
            (team["workspace_id"],),
        ).fetchone()["password_hash"]

    old = stored()
    _change(owner, team["workspace_id"])

    new = stored()
    assert new != old and new.startswith("scrypt$") and NEW_PASSWORD not in new


def test_a_new_password_needs_its_confirmation_and_the_usual_rules(owner, team):
    assert _change(owner, team["workspace_id"], confirm="different-again").status_code == 400
    assert _change(owner, team["workspace_id"], password="short").status_code == 400
    # A refused change leaves the old password working.
    assert (
        owner.get(f"/api/workspaces/{team['workspace_id']}").json()["workspace_code"]
        == team["workspace_code"]
    )


def test_an_admin_cannot_change_the_password_only_the_owner_can(app, db, owner, team):
    admin = sign_in(app, db, "admin@example.com")
    join(admin, team["workspace_code"])
    user = repo.get_user_by_email(db, "admin@example.com")
    assert owner.patch(
        f"/api/workspaces/{team['workspace_id']}/members/{user.user_id}",
        json={"role": "admin"},
    ).status_code == 200

    assert _change(admin, team["workspace_id"]).status_code == 403
    # Nor does an admin see the code.
    assert "workspace_code" not in admin.get(f"/api/workspaces/{team['workspace_id']}").json()


def test_only_owners_are_shown_the_code(app, db, owner, team):
    member = sign_in(app, db, "member@example.com")
    join(member, team["workspace_code"])

    assert owner.get(f"/api/workspaces/{team['workspace_id']}").json()["workspace_code"] == (
        team["workspace_code"]
    )
    listing = member.get("/api/workspaces").json()["workspaces"][0]
    assert "workspace_code" not in listing


def test_the_recorded_owner_follows_a_transfer_of_ownership(app, db, owner, team):
    member = sign_in(app, db, "heir@example.com")
    join(member, team["workspace_code"])
    heir = repo.get_user_by_email(db, "heir@example.com")

    response = owner.post(
        f"/api/workspaces/{team['workspace_id']}/ownership", json={"user_id": heir.user_id}
    )

    assert response.status_code == 200
    row = db.execute(
        "SELECT owner_user_id FROM workspace_access WHERE org_id = ?", (team["workspace_id"],)
    ).fetchone()
    assert row["owner_user_id"] == heir.user_id
    # The new owner sees the code, and the previous one no longer does.
    assert member.get(f"/api/workspaces/{team['workspace_id']}").json()["workspace_code"] == (
        team["workspace_code"]
    )
    assert "workspace_code" not in owner.get(f"/api/workspaces/{team['workspace_id']}").json()


def test_an_owner_can_give_a_workspace_with_no_password_one(app, db):
    """The seeded demo, or one made by a script, is joinable once its owner opts in."""
    owner_id = make_user(db, "scripted@example.com")
    scripted = workspace_service.create_workspace(db, owner_user_id=owner_id, name="Scripted")
    client = sign_in(app, db, "scripted@example.com")

    changed = _change(client, scripted["org_id"])

    assert changed.status_code == 200
    code = changed.json()["workspace_code"]
    assert CODE_SHAPE.match(code)
    assert join(sign_in(app, db, "member@example.com"), code, NEW_PASSWORD).status_code == 200


# --- the audit trail -----------------------------------------------------------------------


def test_joining_and_changing_the_password_are_audited_without_the_secret(
    app, db, owner, team
):
    member = sign_in(app, db, "audited@example.com")
    join(member, team["workspace_code"], "not-the-team-password")
    join(member, team["workspace_code"])
    _change(owner, team["workspace_id"])

    rows = db.execute("SELECT event_type, detail_json FROM audit_log").fetchall()
    events = {row["event_type"] for row in rows}
    assert {
        "org.workspace_joined",
        "org.workspace_join_failed",
        "org.workspace_password_changed",
    } <= events
    dump = " ".join(str(tuple(row)) for row in rows)
    for secret in (WORKSPACE_PASSWORD, NEW_PASSWORD, "not-the-team-password", team["workspace_code"]):
        assert secret not in dump


# --- invitations ---------------------------------------------------------------------------


def test_invitations_are_not_offered_where_addresses_are_not_proven(full_db, db):
    app = create_app(_settings(full_db, require_verified_email=False))
    owner = sign_in(app, db, "owner@example.com")
    team = create_workspace(owner)
    wid = team["workspace_id"]

    listing = owner.get(f"/api/workspaces/{wid}/invitations")
    issued = owner.post(
        f"/api/workspaces/{wid}/invitations", json={"email": "guest@example.com", "role": "viewer"}
    )

    assert listing.status_code == 200 and listing.json()["invitations_enabled"] is False
    assert issued.status_code == 400
    assert "workspace code" in issued.json()["error"]["message"]


def test_invitations_still_work_where_addresses_are_proven(app, db, owner, team):
    wid = team["workspace_id"]

    listing = owner.get(f"/api/workspaces/{wid}/invitations").json()
    issued = owner.post(
        f"/api/workspaces/{wid}/invitations", json={"email": "guest@example.com", "role": "viewer"}
    )

    assert listing["invitations_enabled"] is True
    assert issued.status_code == 201


def test_an_invitation_cannot_be_redeemed_by_an_unverified_account(db, team):
    owner_id = repo.get_user_by_email(db, "owner@example.com").user_id
    _invitation, token = workspace_service.invite_member(
        db,
        org_id=team["workspace_id"],
        email="squatter@example.com",
        role=OrgRole.VIEWER,
        actor_user_id=owner_id,
        actor_role=OrgRole.OWNER,
    )
    squatter = repo.create_user(
        db,
        email="squatter@example.com",
        display_name="squatter",
        password_hash=hash_password(ACCOUNT_PASSWORD),
        email_verified=False,
    )

    with pytest.raises(workspace_service.InvitationError, match="Verify your email"):
        workspace_service.accept_invitation(db, token=token, user_id=squatter.user_id)

    assert repo.get_membership(
        db, org_id=team["workspace_id"], user_id=squatter.user_id
    ) is None
    # The same invitation, redeemed by an account that has proven the address.
    repo.mark_email_verified(db, squatter.user_id)
    workspace_service.accept_invitation(db, token=token, user_id=squatter.user_id)
    assert repo.get_membership(
        db, org_id=team["workspace_id"], user_id=squatter.user_id
    ) is not None


# --- a removed member returning -------------------------------------------------------------
#
# Removing someone deactivates their membership (the row stays, so the audit trail
# keeps its referent), and (org, user) is unique. Coming back with the code and
# password, or with an invitation, therefore has to bring that row back rather
# than insert a second one -- and only ever as the role the way back grants.


def _user_id(db, email: str) -> str:
    return repo.get_user_by_email(db, email).user_id


def _rows(db, workspace_id: str, email: str) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT role, status FROM memberships WHERE org_id = ? AND user_id = ?",
        (workspace_id, _user_id(db, email)),
    ).fetchall()


def _remove(owner: TestClient, db, team: dict, email: str) -> None:
    response = owner.delete(
        f"/api/workspaces/{team['workspace_id']}/members/{_user_id(db, email)}"
    )
    assert response.status_code == 200, response.text


def _promote(owner: TestClient, db, team: dict, email: str, role: str) -> None:
    response = owner.patch(
        f"/api/workspaces/{team['workspace_id']}/members/{_user_id(db, email)}",
        json={"role": role},
    )
    assert response.status_code == 200, response.text


def _reads(client: TestClient, team: dict) -> int:
    return client.get(f"/api/workspaces/{team['workspace_id']}").status_code


def test_a_removed_member_can_rejoin_with_the_code_and_password(app, db, owner, team):
    member = sign_in(app, db, "returning@example.com")
    assert join(member, team["workspace_code"]).status_code == 200
    assert _reads(member, team) == 200

    _remove(owner, db, team, "returning@example.com")
    assert _reads(member, team) == 404  # removed: the workspace is not theirs to see

    rejoined = join(member, team["workspace_code"])

    assert rejoined.status_code == 200, rejoined.text
    assert rejoined.json()["role"] == "viewer"
    assert _reads(member, team) == 200
    rows = _rows(db, team["workspace_id"], "returning@example.com")
    assert [(r["role"], r["status"]) for r in rows] == [("viewer", "active")]


def test_rejoining_does_not_restore_a_previous_elevated_role(app, db, owner, team):
    member = sign_in(app, db, "was-admin@example.com")
    join(member, team["workspace_code"])
    _promote(owner, db, team, "was-admin@example.com", "admin")
    assert member.get(f"/api/workspaces/{team['workspace_id']}/invitations").status_code == 200
    _remove(owner, db, team, "was-admin@example.com")

    assert join(member, team["workspace_code"]).status_code == 200

    view = member.get(f"/api/workspaces/{team['workspace_id']}").json()
    assert view["role"] == "viewer"
    assert "workspace_code" not in view
    # What an admin may do, they may not do now.
    assert member.get(f"/api/workspaces/{team['workspace_id']}/invitations").status_code == 403
    assert _change(member, team["workspace_id"]).status_code == 403
    assert [r["role"] for r in _rows(db, team["workspace_id"], "was-admin@example.com")] == [
        "viewer"
    ]


def test_a_removed_member_cannot_come_back_with_a_wrong_password(app, db, owner, team):
    member = sign_in(app, db, "wrongpw@example.com")
    join(member, team["workspace_code"])
    _remove(owner, db, team, "wrongpw@example.com")

    response = join(member, team["workspace_code"], "not-the-team-password")

    assert response.status_code == 400
    assert response.json()["error"]["message"] == REFUSAL
    assert _reads(member, team) == 404
    assert [r["status"] for r in _rows(db, team["workspace_id"], "wrongpw@example.com")] == [
        "removed"
    ]


def test_a_removed_member_cannot_come_back_with_a_wrong_code(app, db, owner, team):
    member = sign_in(app, db, "wrongcode@example.com")
    join(member, team["workspace_code"])
    _remove(owner, db, team, "wrongcode@example.com")

    response = join(member, "ZZZZZZZZZZ")

    assert response.status_code == 400
    assert response.json()["error"]["message"] == REFUSAL
    assert _reads(member, team) == 404
    assert [r["status"] for r in _rows(db, team["workspace_id"], "wrongcode@example.com")] == [
        "removed"
    ]


def test_rejoining_still_needs_a_signed_in_account(app, db, owner, team):
    member = sign_in(app, db, "signedout@example.com")
    join(member, team["workspace_code"])
    _remove(owner, db, team, "signedout@example.com")

    stranger = TestClient(app)  # no session
    response = join(stranger, team["workspace_code"])

    assert response.status_code == 401
    assert [r["status"] for r in _rows(db, team["workspace_id"], "signedout@example.com")] == [
        "removed"
    ]


def test_rejoining_is_still_throttled(app, db, owner, team):
    member = sign_in(app, db, "throttled@example.com")
    join(member, team["workspace_code"])
    _remove(owner, db, team, "throttled@example.com")
    for _ in range(workspace_service.MAX_JOIN_FAILURES_PER_USER):
        assert join(member, team["workspace_code"], "guess-guess-guess").status_code == 400

    locked = join(member, team["workspace_code"])  # even the right answer

    assert locked.status_code == 429
    assert _reads(member, team) == 404


def test_the_old_password_does_not_bring_a_removed_member_back_after_a_change(
    app, db, owner, team
):
    member = sign_in(app, db, "rotated@example.com")
    join(member, team["workspace_code"])
    _remove(owner, db, team, "rotated@example.com")
    assert _change(owner, team["workspace_id"]).status_code == 200

    assert join(member, team["workspace_code"], WORKSPACE_PASSWORD).status_code == 400
    assert _reads(member, team) == 404
    assert join(member, team["workspace_code"], NEW_PASSWORD).status_code == 200


def test_rejoining_repeatedly_and_being_removed_again_never_adds_a_row(app, db, owner, team):
    member = sign_in(app, db, "cycle@example.com")
    for _ in range(3):
        assert join(member, team["workspace_code"]).status_code == 200
        assert join(member, team["workspace_code"]).status_code == 200  # idempotent
        _remove(owner, db, team, "cycle@example.com")

    assert join(member, team["workspace_code"]).status_code == 200
    assert len(_rows(db, team["workspace_id"], "cycle@example.com")) == 1


def test_a_rejoin_is_audited_without_the_secret(app, db, owner, team):
    member = sign_in(app, db, "audit-rejoin@example.com")
    join(member, team["workspace_code"])
    _remove(owner, db, team, "audit-rejoin@example.com")
    join(member, team["workspace_code"])

    rows = db.execute(
        "SELECT event_type, detail_json FROM audit_log"
        " WHERE event_type = 'org.membership_created' AND target_id = ?"
        " ORDER BY rowid",
        (_user_id(db, "audit-rejoin@example.com"),),
    ).fetchall()

    assert len(rows) == 2
    assert "rejoined" not in rows[0]["detail_json"]
    assert "rejoined" in rows[1]["detail_json"]
    dump = " ".join(str(tuple(row)) for row in rows)
    assert WORKSPACE_PASSWORD not in dump and team["workspace_code"] not in dump


def test_a_removed_member_cannot_touch_the_workspace_before_rejoining(app, db, owner, team):
    member = sign_in(app, db, "shut-out@example.com")
    join(member, team["workspace_code"])
    _remove(owner, db, team, "shut-out@example.com")

    for path in (
        f"/api/workspaces/{team['workspace_id']}",
        f"/api/workspaces/{team['workspace_id']}/members",
        f"/api/workspaces/{team['workspace_id']}/invitations",
    ):
        assert member.get(path).status_code == 404, path


def _returning_guest(db, team: dict, email: str, *, was: OrgRole) -> str:
    guest = repo.create_user(
        db,
        email=email,
        display_name=email.split("@")[0],
        password_hash=hash_password(ACCOUNT_PASSWORD),
        email_verified=True,
    )
    repo.add_member(db, org_id=team["workspace_id"], user_id=guest.user_id, role=was)
    assert repo.remove_member(db, org_id=team["workspace_id"], user_id=guest.user_id)
    assert repo.get_membership(db, org_id=team["workspace_id"], user_id=guest.user_id) is None
    return guest.user_id


def _invite(db, team: dict, email: str, role: OrgRole) -> str:
    _invitation, token = workspace_service.invite_member(
        db,
        org_id=team["workspace_id"],
        email=email,
        role=role,
        actor_user_id=_user_id(db, "owner@example.com"),
        actor_role=OrgRole.OWNER,
    )
    return token


def test_an_invitation_reactivates_a_removed_member_instead_of_duplicating_them(db, owner, team):
    guest_id = _returning_guest(db, team, "guest@example.com", was=OrgRole.ADMIN)
    token = _invite(db, team, "guest@example.com", OrgRole.VIEWER)

    workspace_service.accept_invitation(db, token=token, user_id=guest_id)

    rows = _rows(db, team["workspace_id"], "guest@example.com")
    assert [(r["role"], r["status"]) for r in rows] == [("viewer", "active")]
    membership = repo.get_membership(db, org_id=team["workspace_id"], user_id=guest_id)
    assert membership is not None and membership.role is OrgRole.VIEWER  # not the old admin


def test_an_invitation_grants_the_invited_role_to_a_returning_member(db, owner, team):
    guest_id = _returning_guest(db, team, "promoted@example.com", was=OrgRole.VIEWER)
    token = _invite(db, team, "promoted@example.com", OrgRole.OPERATIONS)

    workspace_service.accept_invitation(db, token=token, user_id=guest_id)

    rows = _rows(db, team["workspace_id"], "promoted@example.com")
    assert [(r["role"], r["status"]) for r in rows] == [("operations", "active")]


def test_reactivation_leaves_an_active_membership_alone(db, owner, team):
    owner_id = _user_id(db, "owner@example.com")

    changed = repo.reactivate_member(
        db, org_id=team["workspace_id"], user_id=owner_id, role=OrgRole.VIEWER
    )

    assert changed is False
    assert [(r["role"], r["status"]) for r in _rows(db, team["workspace_id"], "owner@example.com")] == [
        ("owner", "active")
    ]
