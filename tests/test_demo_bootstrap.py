"""The self-healing public demo: one click in, and nothing to run by hand.

The deployment this covers sleeps when it is idle and wakes with an empty
disk. Everything here is a question about that: what the application does when
the database it needs is simply not there, what it does when the database *is*
there, and what it must never do in either case.

Three properties are the subject, and each has a failure mode worth naming:

- **Convergence, not initialisation.** `ensure_demo_environment` is not a
  first-boot step; it is something the application does every time and which
  costs nothing when there is nothing to do. The failure mode it exists to
  rule out is a process-memory flag — a server that "knows" it initialised the
  database, on a filesystem that has been replaced since.
- **Idempotence under repetition and under concurrency.** Two visitors landing
  on a cold instance at the same moment must produce one workspace between
  them, not two halves of one each.
- **The credential stays on the server.** The endpoint takes no body, so there
  is nothing to substitute; no response and no log line carries the password.

Everything runs over HTTP where a visitor would meet it, because "can somebody
who has never seen this application get in" is an HTTP question.
"""

from __future__ import annotations

import logging
import pathlib
import sqlite3
import threading

import pytest

# The demo environment builds and seeds its own local SQLite file; it is not
# available against PostgreSQL (Settings.validate_auth refuses the combination).
pytestmark = pytest.mark.sqlite_only
from fastapi.testclient import TestClient

from app.backend.api.app import create_app
from app.backend.core.config import (
    DEFAULT_DEMO_EMAIL,
    DEFAULT_DEMO_PASSWORD,
    AuthMode,
    Settings,
    load_settings,
)
from app.backend.services.bootstrap import (
    DemoEnvironmentError,
    demo_environment_ready,
    ensure_demo_environment,
    repair_demo_credential,
)
from app.backend.services.database import get_connection, initialize_schema
from scripts.seed_demo import DEMO_USERS, DEMO_WORKSPACE_SLUG

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The source pack the bootstrap ingests from. Present in the repository, and
#: copied into the container image, so this is not an environmental assumption.
SOURCE_DIR = REPO_ROOT / "data" / "source"


@pytest.fixture
def db_path(tmp_path) -> pathlib.Path:
    """A path where no database exists. The cold start, exactly."""
    return tmp_path / "astrion.db"


def settings_for(db_path: pathlib.Path, **overrides) -> Settings:
    base = {
        "database_path": db_path,
        "auth_mode": AuthMode.SESSION,
        "cors_allow_origins": (),
        "session_cookie_secure": False,
        "rate_limit_enabled": False,
        "demo_login_enabled": True,
    }
    base.update(overrides)
    return Settings(**base)


def open_db(db_path: pathlib.Path) -> sqlite3.Connection:
    conn = get_connection(db_path)
    initialize_schema(conn)
    return conn


def count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


def snapshot(db_path: pathlib.Path) -> dict[str, int]:
    """Row counts for everything a duplicate would show up in."""
    conn = get_connection(db_path)
    try:
        counted = {
            table: count(conn, table)
            for table in (
                "users",
                "memberships",
                "organization_accounts",
                "accounts",
                "orders",
                "tickets",
                "documents",
                "document_chunks",
            )
        }
        # "organizations" means demo tenants: workspaces somebody can sign in to.
        counted["organizations"] = workspaces_with_members(conn)
        return counted
    finally:
        conn.close()


def workspaces_with_members(conn: sqlite3.Connection) -> int:
    """Workspaces somebody can sign in to. The imported dataset also leaves an
    empty legacy workspace behind, which is not a duplicate demo tenant."""
    return int(
        conn.execute("SELECT COUNT(DISTINCT org_id) AS n FROM memberships").fetchone()["n"]
    )


def demo_user_id(db_path: pathlib.Path) -> str:
    """The stored id of the configured demo account.

    `/api/auth/me` reports an id, not an address — deliberately, since the
    address is not what anything downstream is keyed on. Resolving it here is
    what lets a test say "the session belongs to the demo account" rather than
    "the session belongs to whoever the server felt like".
    """
    from app.backend.auth import repository as repo

    conn = get_connection(db_path)
    try:
        user = repo.get_user_by_email(conn, DEFAULT_DEMO_EMAIL)
        assert user is not None
        return user.user_id
    finally:
        conn.close()


def cold_client(settings: Settings) -> TestClient:
    """A client whose application has *not* run its startup hook.

    Deliberate: the startup hook is an optimisation, and every guarantee this
    suite is about has to hold without it. Entering the context manager would
    run the lifespan and quietly do the work under test before the test began.
    """
    return TestClient(create_app(settings))


# ===========================================================================
# A cold start
# ===========================================================================


def test_a_fresh_database_is_built_by_the_first_demo_sign_in(db_path):
    """Requirement one: no database, no schema, no data — and one click in."""
    assert not db_path.exists()

    client = cold_client(settings_for(db_path))
    response = client.post("/api/auth/demo-login")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "authenticated"
    assert db_path.exists()

    counts = snapshot(db_path)
    assert counts["accounts"] > 0
    assert counts["orders"] > 0
    assert counts["tickets"] > 0
    assert counts["documents"] > 0
    assert counts["document_chunks"] > 0
    assert counts["organizations"] == 1
    assert counts["memberships"] == len(DEMO_USERS)


def test_the_session_it_issues_actually_works(db_path):
    """A sign-in that authenticates nothing would pass every check above."""
    client = cold_client(settings_for(db_path))
    client.post("/api/auth/demo-login")

    me = client.get("/api/auth/me")
    assert me.status_code == 200, me.text
    assert me.json()["user_id"] == demo_user_id(db_path)

    workspaces = client.get("/api/workspaces")
    assert workspaces.status_code == 200
    body = workspaces.json()
    assert body["needs_workspace"] is False
    assert body["active_workspace_id"] is not None


def test_the_visitor_lands_on_the_demo_workspace_with_its_data(db_path):
    """Signed in and looking at an empty product is not "in"."""
    client = cold_client(settings_for(db_path))
    client.post("/api/auth/demo-login")

    workspaces = client.get("/api/workspaces").json()["workspaces"]
    assert len(workspaces) == 1
    assert workspaces[0]["slug"] == DEMO_WORKSPACE_SLUG

    conn = get_connection(db_path)
    try:
        assert count(conn, "organization_accounts") > 0
    finally:
        conn.close()


def test_the_startup_hook_prepares_the_environment_before_anyone_clicks(db_path):
    """So the sign-in page is right on arrival, not merely fixable on click.

    Without this, the page whose only job is to let somebody in greets the
    first visitor after a cold start with an error about a missing database.
    """
    settings = settings_for(db_path)
    with TestClient(create_app(settings)) as client:
        assert db_path.exists()
        # The symptom this whole change exists to remove: `/api/auth/me` used
        # to answer 503 "the database has not been built" here.
        assert client.get("/api/auth/me").status_code == 401
        health = client.get("/health").json()

    assert health["database_ready"] is True
    assert health["demo_login_enabled"] is True


def test_a_failing_bootstrap_does_not_stop_the_server_starting(db_path, monkeypatch):
    """An operator cannot read a log that a crash-loop never gets to write."""
    monkeypatch.setattr(
        "app.backend.api.app.ensure_demo_environment",
        lambda _settings: (_ for _ in ()).throw(DemoEnvironmentError("dataset", "boom")),
    )
    with TestClient(create_app(settings_for(db_path))) as client:
        assert client.get("/health").status_code == 200


# ===========================================================================
# Doing nothing, well
# ===========================================================================


def test_a_second_sign_in_duplicates_nothing(db_path):
    client = cold_client(settings_for(db_path))
    assert client.post("/api/auth/demo-login").status_code == 200
    first = snapshot(db_path)

    assert client.post("/api/auth/demo-login").status_code == 200
    assert snapshot(db_path) == first


def test_signing_in_repeatedly_stays_safe(db_path):
    """Five clicks, which is what a visitor who reloads the page produces."""
    client = cold_client(settings_for(db_path))
    client.post("/api/auth/demo-login")
    first = snapshot(db_path)

    for _ in range(4):
        assert client.post("/api/auth/demo-login").status_code == 200

    assert snapshot(db_path) == first


def test_an_already_built_environment_re_ingests_nothing(db_path):
    """The check is "are the rows there", so the answer must cost nothing.

    Asserted by making re-ingestion fail loudly: if the ordinary path touched
    it, this test would raise rather than pass.
    """
    settings = settings_for(db_path)
    ensure_demo_environment(settings)

    import scripts.ingest_dataset as ingest_dataset
    import scripts.ingest_documents as ingest_documents

    def refuse(*args, **kwargs):
        raise AssertionError("ingestion re-ran against an already-built database")

    original = (ingest_dataset.ingest, ingest_documents.ingest)
    ingest_dataset.ingest, ingest_documents.ingest = refuse, refuse
    try:
        report = ensure_demo_environment(settings)
    finally:
        ingest_dataset.ingest, ingest_documents.ingest = original

    assert report.already_ready is True
    assert report.changed is False


def test_an_existing_demo_user_is_reused_rather_than_duplicated(db_path):
    settings = settings_for(db_path)
    ensure_demo_environment(settings)

    conn = get_connection(db_path)
    try:
        before = [
            row["user_id"]
            for row in conn.execute("SELECT user_id FROM users ORDER BY email")
        ]
    finally:
        conn.close()

    report = ensure_demo_environment(settings)
    assert report.users_created == ()

    conn = get_connection(db_path)
    try:
        after = [
            row["user_id"]
            for row in conn.execute("SELECT user_id FROM users ORDER BY email")
        ]
    finally:
        conn.close()
    # Identity preserved, not merely count preserved: a recreated user with a
    # new id would orphan every audit row that names the old one.
    assert after == before


def test_an_existing_demo_workspace_is_reused_rather_than_duplicated(db_path):
    settings = settings_for(db_path)
    ensure_demo_environment(settings)

    conn = get_connection(db_path)
    try:
        before = conn.execute(
            "SELECT org_id FROM organizations WHERE slug = ?", (DEMO_WORKSPACE_SLUG,)
        ).fetchone()["org_id"]
    finally:
        conn.close()

    report = ensure_demo_environment(settings)
    assert report.workspace_created is False

    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT org_id, slug FROM organizations "
            "WHERE org_id IN (SELECT org_id FROM memberships)"
        ).fetchall()
    finally:
        conn.close()
    assert [row["slug"] for row in rows] == [DEMO_WORKSPACE_SLUG]
    assert rows[0]["org_id"] == before


# ===========================================================================
# Completing a half-built environment
# ===========================================================================


def test_a_missing_dataset_is_restored_without_touching_the_documents(db_path):
    """Partial loss is the normal shape of a half-finished boot."""
    settings = settings_for(db_path)
    ensure_demo_environment(settings)
    before = snapshot(db_path)

    conn = get_connection(db_path)
    try:
        with conn:
            conn.execute("DELETE FROM orders")
            conn.execute("DELETE FROM tickets")
            conn.execute("DELETE FROM accounts")
            conn.execute("DELETE FROM dataset_metadata")
        assert not demo_environment_ready(conn, settings)
    finally:
        conn.close()

    report = ensure_demo_environment(settings)
    assert report.dataset_ingested is True
    assert report.documents_ingested is False

    after = snapshot(db_path)
    for table in ("accounts", "orders", "tickets", "documents", "document_chunks"):
        assert after[table] == before[table], table


def test_a_missing_document_index_is_restored_without_re_ingesting_the_dataset(
    db_path,
):
    settings = settings_for(db_path)
    ensure_demo_environment(settings)
    before = snapshot(db_path)

    conn = get_connection(db_path)
    try:
        with conn:
            conn.execute("DELETE FROM document_chunks")
            conn.execute("DELETE FROM documents")
        assert not demo_environment_ready(conn, settings)
    finally:
        conn.close()

    report = ensure_demo_environment(settings)
    assert report.documents_ingested is True
    assert report.dataset_ingested is False

    after = snapshot(db_path)
    assert after["documents"] == before["documents"]
    assert after["document_chunks"] == before["document_chunks"]


def test_chunks_without_documents_still_count_as_missing(db_path):
    """A document row with nothing under it answers every question with silence."""
    settings = settings_for(db_path)
    ensure_demo_environment(settings)

    conn = get_connection(db_path)
    try:
        with conn:
            conn.execute("DELETE FROM document_chunks")
        assert not demo_environment_ready(conn, settings)
    finally:
        conn.close()

    assert ensure_demo_environment(settings).documents_ingested is True


def test_a_database_with_data_but_no_demo_tenant_gains_only_the_tenant(db_path):
    """The other half of partial: ingested, never seeded."""
    import scripts.ingest_dataset as ingest_dataset
    import scripts.ingest_documents as ingest_documents

    ingest_dataset.ingest(db_path=db_path)
    ingest_documents.ingest(db_path=db_path)
    before = snapshot(db_path)

    report = ensure_demo_environment(settings_for(db_path))

    assert report.dataset_ingested is False
    assert report.documents_ingested is False
    assert report.workspace_created is True

    after = snapshot(db_path)
    assert after["accounts"] == before["accounts"]
    assert after["organizations"] == 1


def test_a_stale_stored_password_is_repaired_rather_than_refused(db_path):
    """A database carried across a password change must not brick the demo."""
    settings = settings_for(db_path)
    ensure_demo_environment(settings)

    from app.backend.auth import repository as repo
    from app.backend.auth.passwords import hash_password

    conn = get_connection(db_path)
    try:
        user = repo.get_user_by_email(conn, DEFAULT_DEMO_EMAIL)
        repo.set_password_hash(conn, user.user_id, hash_password("a-different-one"))
    finally:
        conn.close()

    client = cold_client(settings)
    assert client.post("/api/auth/demo-login").status_code == 200
    assert client.get("/api/auth/me").status_code == 200


def test_the_repair_touches_only_the_configured_demo_address(db_path):
    """It is not a password reset facility, and has no address to be given one."""
    settings = settings_for(db_path)
    ensure_demo_environment(settings)

    from app.backend.auth import repository as repo
    from app.backend.auth.passwords import hash_password, verify_password

    conn = get_connection(db_path)
    try:
        other = repo.create_user(
            conn,
            email="real.person@example.com",
            display_name="Real Person",
            password_hash=hash_password("their-own-password"),
            email_verified=True,
        )
        repo.set_password_hash(
            conn,
            repo.get_user_by_email(conn, DEFAULT_DEMO_EMAIL).user_id,
            hash_password("a-stale-password"),
        )

        assert repair_demo_credential(conn, settings) is True

        untouched = repo.get_user(conn, other.user_id)
        assert verify_password("their-own-password", untouched.password_hash)
    finally:
        conn.close()


# ===========================================================================
# Two visitors at once
# ===========================================================================


def test_concurrent_demo_sign_ins_produce_one_environment(db_path):
    """Eight simultaneous first clicks on a cold instance."""
    client = cold_client(settings_for(db_path))
    results: list[int] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def click() -> None:
        try:
            barrier.wait(timeout=30)
            results.append(client.post("/api/auth/demo-login").status_code)
        except BaseException as exc:  # noqa: BLE001 — recorded and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=click) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert errors == []
    assert results == [200] * 8

    counts = snapshot(db_path)
    assert counts["organizations"] == 1
    assert counts["users"] == len(DEMO_USERS)
    assert counts["memberships"] == len(DEMO_USERS)
    # One row per account, not eight: the unique index is the guarantee, and
    # the lock is what stops eight callers finding out about it the hard way.
    assert counts["organization_accounts"] == counts["accounts"]


def test_concurrent_bootstraps_leave_no_duplicate_records(db_path):
    """The same race one level down, without HTTP in the way."""
    settings = settings_for(db_path)
    reports = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(6)

    def build() -> None:
        try:
            barrier.wait(timeout=30)
            reports.append(ensure_demo_environment(settings))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=build) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert errors == []
    assert len(reports) == 6
    # Exactly one caller does the work; the rest find it done.
    assert sum(1 for report in reports if report.changed) == 1

    counts = snapshot(db_path)
    assert counts["organizations"] == 1
    assert counts["users"] == len(DEMO_USERS)
    assert counts["memberships"] == len(DEMO_USERS)


def test_a_stale_lock_file_is_broken_rather_than_waited_out(db_path, monkeypatch):
    """A process that died holding the lock must not close the demo forever."""
    from app.backend.services import bootstrap

    lock = bootstrap._lock_path(db_path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("99999", encoding="utf-8")
    # Older than any real bootstrap, without waiting for one to age.
    monkeypatch.setattr(bootstrap, "LOCK_STALE_SECONDS", -1.0)

    assert ensure_demo_environment(settings_for(db_path)).changed is True
    assert not lock.exists()


def test_the_lock_file_is_removed_when_the_bootstrap_fails(db_path, monkeypatch):
    from app.backend.services import bootstrap

    def explode(db_path=None, **kwargs):
        raise RuntimeError("the workbook is corrupt")

    monkeypatch.setattr(
        bootstrap,
        "_import_ingestion",
        lambda: (type("M", (), {"ingest": staticmethod(explode)}), None),
    )

    with pytest.raises(DemoEnvironmentError) as caught:
        ensure_demo_environment(settings_for(db_path))

    assert caught.value.stage == "dataset"
    assert not bootstrap._lock_path(db_path).exists()


# ===========================================================================
# Nothing is remembered
# ===========================================================================


def test_readiness_is_re_derived_from_the_database_every_time(db_path):
    """The cold-start requirement, stated as the thing that would break it.

    A process that has already bootstrapped successfully, whose filesystem is
    then replaced underneath it, must rebuild. An `INITIALIZED = True` would
    pass every other test in this file and fail this one.
    """
    settings = settings_for(db_path)
    client = cold_client(settings)
    assert client.post("/api/auth/demo-login").status_code == 200

    # The platform recycled the instance's disk. Same process, same app object.
    db_path.unlink()
    assert not db_path.exists()

    assert client.post("/api/auth/demo-login").status_code == 200
    assert db_path.exists()
    assert snapshot(db_path)["organizations"] == 1


def test_a_sign_in_after_the_data_is_wiped_still_answers(db_path):
    """The deployed symptom, reproduced: idle, wiped, and then a visitor."""
    settings = settings_for(db_path)
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/auth/demo-login").status_code == 200
        db_path.unlink()
        # No restart, no redeploy, nobody running anything.
        response = client.post("/api/auth/demo-login")

    assert response.status_code == 200
    assert response.json()["status"] == "authenticated"


# ===========================================================================
# The credential stays on the server
# ===========================================================================


def test_the_endpoint_accepts_no_identity_from_the_caller(db_path):
    """There is no field in which to ask to be somebody else.

    The strongest form of "a visitor cannot choose their workspace through the
    demo endpoint": not a rejected field, but no field.
    """
    client = cold_client(settings_for(db_path))
    response = client.post(
        "/api/auth/demo-login",
        json={"email": "owner@demo.astrion.example", "password": "anything"},
    )

    assert response.status_code == 200
    # Whatever the body said, the session is the configured demo identity —
    # not the owner account the caller asked for.
    assert client.get("/api/auth/me").json()["user_id"] == demo_user_id(db_path)


def test_no_response_carries_the_demo_password(db_path):
    client = cold_client(settings_for(db_path))

    bodies = [
        client.post("/api/auth/demo-login").text,
        client.get("/health").text,
        client.get("/api/auth/me").text,
        client.get("/api/workspaces").text,
        client.get("/openapi.json").text,
    ]

    for body in bodies:
        assert DEFAULT_DEMO_PASSWORD not in body


def test_the_session_travels_only_as_an_httponly_cookie(db_path):
    client = cold_client(settings_for(db_path))
    response = client.post("/api/auth/demo-login")

    assert "session_token" not in response.text
    header = response.headers["set-cookie"]
    assert "HttpOnly" in header
    assert "astrion_session=" in header


def test_no_log_line_contains_the_demo_password(db_path, caplog):
    with caplog.at_level(logging.DEBUG):
        client = cold_client(settings_for(db_path))
        client.post("/api/auth/demo-login")
        # And the repair path, which is the one that handles it directly.
        from app.backend.auth import repository as repo
        from app.backend.auth.passwords import hash_password

        conn = get_connection(db_path)
        try:
            repo.set_password_hash(
                conn,
                repo.get_user_by_email(conn, DEFAULT_DEMO_EMAIL).user_id,
                hash_password("a-stale-password"),
            )
        finally:
            conn.close()
        client.post("/api/auth/demo-login")

    assert DEFAULT_DEMO_PASSWORD not in caplog.text


def test_the_frontend_bundle_contains_no_demo_credential():
    """The password must not be reachable from anything shipped to a browser.

    Checked across the frontend source rather than at one import site: the
    previous design published it through `NEXT_PUBLIC_*`, which Next.js inlines
    into the client bundle, so the way this regresses is a new variable rather
    than a changed line.
    """
    frontend = REPO_ROOT / "app" / "frontend" / "src"
    for path in sorted(frontend.rglob("*.ts*")):
        text = path.read_text(encoding="utf-8")
        assert DEFAULT_DEMO_PASSWORD not in text, path
        assert "NEXT_PUBLIC_DEMO_PASSWORD" not in text, path


# ===========================================================================
# What it must not change
# ===========================================================================


def test_the_configured_demo_address_is_one_the_seed_actually_creates(db_path):
    """Two files have to agree, and only one of them can be wrong quietly."""
    assert DEFAULT_DEMO_EMAIL in {email for email, _name, _role in DEMO_USERS}


def test_an_ordinary_sign_in_is_unaffected(db_path):
    """The demo endpoint is an addition, not a replacement."""
    settings = settings_for(db_path)
    ensure_demo_environment(settings)
    client = cold_client(settings)

    good = client.post(
        "/api/auth/login",
        json={"email": DEFAULT_DEMO_EMAIL, "password": settings.demo_password},
    )
    assert good.status_code == 200
    assert good.json()["status"] == "authenticated"

    bad = TestClient(create_app(settings)).post(
        "/api/auth/login",
        json={"email": DEFAULT_DEMO_EMAIL, "password": "not-the-password"},
    )
    assert bad.status_code == 401


def test_registration_still_works_and_still_refuses_an_unverified_sign_in(db_path):
    settings = settings_for(db_path)
    ensure_demo_environment(settings)
    client = cold_client(settings)

    registered = client.post(
        "/api/auth/register",
        json={
            "email": "new.person@example.com",
            "password": "a-good-enough-password",
            "display_name": "New Person",
        },
    )
    assert registered.status_code == 200

    refused = client.post(
        "/api/auth/login",
        json={"email": "new.person@example.com", "password": "a-good-enough-password"},
    )
    assert refused.status_code == 401


def test_the_demo_endpoint_is_absent_when_the_deployment_did_not_publish_one(db_path):
    """A self-hosted copy must not seed a public workspace into its database."""
    settings = settings_for(db_path, demo_login_enabled=False)
    client = cold_client(settings)

    response = client.post("/api/auth/demo-login")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert not db_path.exists()


def test_health_reports_whether_the_demo_is_offered(db_path):
    """The frontend has to learn it from the server, never from a build flag."""
    enabled = cold_client(settings_for(db_path)).get("/health").json()
    assert enabled["demo_login_enabled"] is True

    disabled = (
        cold_client(settings_for(db_path, demo_login_enabled=False))
        .get("/health")
        .json()
    )
    assert disabled["demo_login_enabled"] is False


def test_the_demo_endpoint_is_refused_under_the_identity_header_mode(db_path):
    """In that mode a session is a thing nothing consults."""
    settings = settings_for(db_path, auth_mode=AuthMode.DEMO_HEADER)
    response = cold_client(settings).post("/api/auth/demo-login")
    assert response.status_code == 403


def test_the_demo_user_is_an_ordinary_member_with_an_ordinary_role(db_path):
    """No demo-only permission exists, and this endpoint cannot invent one."""
    from app.backend.auth.permissions import OrgRole, permissions_for

    client = cold_client(settings_for(db_path))
    client.post("/api/auth/demo-login")

    workspace = client.get("/api/workspaces").json()["workspaces"][0]
    role = OrgRole(workspace["role"])
    assert set(workspace["permissions"]) == {
        permission.value for permission in permissions_for(role)
    }


def test_the_bootstrap_attaches_no_account_another_workspace_owns(db_path):
    """The unique index is the tenant boundary, and it is not a boot decision."""
    from app.backend.auth import repository as repo
    from app.backend.auth import workspaces as workspace_service
    from app.backend.auth.passwords import hash_password

    import scripts.ingest_dataset as ingest_dataset
    import scripts.ingest_documents as ingest_documents

    ingest_dataset.ingest(db_path=db_path)
    ingest_documents.ingest(db_path=db_path)

    conn = get_connection(db_path)
    try:
        owner = repo.create_user(
            conn,
            email="owner@real-tenant.example",
            display_name="Real Owner",
            password_hash=hash_password("their-own-password"),
            email_verified=True,
        )
        claimed = conn.execute(
            "SELECT account_id FROM accounts ORDER BY account_id"
        ).fetchone()["account_id"]
        real = workspace_service.create_workspace(
            conn, owner_user_id=owner.user_id, name="Real Tenant", account_ids=[claimed]
        )
    finally:
        conn.close()

    report = ensure_demo_environment(settings_for(db_path))

    # Already moved out of the imported dataset into the real tenant's workspace:
    # not the bootstrap's to attach, and not taken back.
    assert claimed not in report.accounts_attached

    conn = get_connection(db_path)
    try:
        assert repo.org_owning_account(conn, claimed) == real["org_id"]
    finally:
        conn.close()


# ===========================================================================
# Configuration
# ===========================================================================


def test_the_deployment_default_is_on_and_the_model_default_is_off(monkeypatch):
    """Two defaults, on purpose, and a test so neither drifts silently.

    A `Settings` constructed directly describes one configuration and must not
    acquire a demo workspace it did not ask for. A `Settings` loaded from the
    environment is a deployment of this application, which is a public demo.
    """
    monkeypatch.delenv("DEMO_LOGIN_ENABLED", raising=False)
    assert Settings().demo_login_enabled is False
    assert load_settings().demo_login_enabled is True

    monkeypatch.setenv("DEMO_LOGIN_ENABLED", "false")
    assert load_settings().demo_login_enabled is False


def test_a_deployment_may_supply_its_own_demo_password(monkeypatch):
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    monkeypatch.delenv("DEMO_SEED_PASSWORD", raising=False)
    assert load_settings().demo_password == DEFAULT_DEMO_PASSWORD

    # The name `docker-entrypoint.sh` already reads, so a Docker boot and an
    # in-process bootstrap cannot disagree about which password was seeded.
    monkeypatch.setenv("DEMO_SEED_PASSWORD", "from-the-container")
    assert load_settings().demo_password == "from-the-container"

    monkeypatch.setenv("DEMO_PASSWORD", "more-specific-still")
    assert load_settings().demo_password == "more-specific-still"


def test_the_demo_password_is_not_in_the_configuration_a_response_carries():
    summary = Settings(demo_login_enabled=True).public_summary()
    assert "demo_password" not in summary
    assert DEFAULT_DEMO_PASSWORD not in str(summary)
    assert summary["demo_login_enabled"] is True


def test_the_demo_password_stays_out_of_a_settings_repr():
    assert DEFAULT_DEMO_PASSWORD not in repr(Settings(demo_login_enabled=True))


def test_the_source_pack_the_bootstrap_depends_on_is_in_the_repository():
    """The bootstrap ingests from files; a deployment must actually ship them."""
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "data/source"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    expected = {path.name for path in SOURCE_DIR.iterdir() if path.suffix in {".pdf", ".xlsx"}}
    assert expected
    assert expected <= {pathlib.PurePosixPath(p).name for p in tracked}


# ===========================================================================
# The one-click visitor can walk the whole confirmation gate
#
# A support visitor could watch an action being prepared and never see it
# confirmed: support may only propose, and a proposal is confirmed only by the
# person who prepared it. The one-click identity is therefore the operations
# member, which is exactly support plus execution and the audit trail. These
# tests are the proof that nothing *else* moved with it.
# ===========================================================================


def demo_visitor(db_path):
    client = cold_client(settings_for(db_path))
    assert client.post("/api/auth/demo-login").status_code == 200
    return client


def prepare(client, message):
    body = client.post("/api/chat", json={"message": message}).json()
    assert body["action_status"] == "pending_confirmation", body["answer"]
    return body, body["proposed_action"]


def confirm(client, body, proposal, **overrides):
    payload = {
        "decision": "approve",
        "session_id": body["session_id"],
        "expected_fingerprint": proposal["parameter_fingerprint"],
        **overrides,
    }
    return client.post(f"/api/actions/{proposal['action_id']}/confirm", json=payload)


def action_status(db_path, action_id):
    conn = get_connection(db_path)
    try:
        return conn.execute(
            "SELECT status FROM agent_actions WHERE action_id = ?", (action_id,)
        ).fetchone()["status"]
    finally:
        conn.close()


ESCALATE = "Investigate TKT-501 and escalate it if the outage warrants it."
CREDIT = "ORD-2002 missed its pickup window. Prepare a service credit for it."


def test_the_one_click_identity_is_the_operations_member_and_nothing_more(db_path):
    from app.backend.auth.permissions import OrgRole, Permission, permissions_for

    workspace = demo_visitor(db_path).get("/api/workspaces").json()["workspaces"][0]

    assert workspace["role"] == OrgRole.OPERATIONS.value
    granted = set(workspace["permissions"])
    assert granted == {p.value for p in permissions_for(OrgRole.OPERATIONS)}
    assert Permission.EXECUTE_ACTION.value in granted
    assert Permission.READ_AUDIT_LOG.value in granted
    for withheld in (
        Permission.APPROVE_HIGH_VALUE_ACTION,
        Permission.MANAGE_DOCUMENTS,
        Permission.MEMBERS_INVITE,
        Permission.MEMBERS_REMOVE,
        Permission.MEMBERS_CHANGE_ROLE,
        Permission.WORKSPACE_UPDATE,
        Permission.WORKSPACE_DELETE,
    ):
        assert withheld.value not in granted, withheld


def test_a_demo_visitor_prepares_then_confirms_an_escalation(db_path):
    visitor = demo_visitor(db_path)
    body, proposal = prepare(visitor, ESCALATE)
    assert proposal["action_type"] == "create_escalation"
    # Prepared is not performed: the chat turn changed nothing.
    assert action_status(db_path, proposal["action_id"]) == "pending_confirmation"

    response = confirm(visitor, body, proposal)

    assert response.status_code == 200, response.text
    assert response.json()["action_status"] == "executed"
    assert action_status(db_path, proposal["action_id"]) == "executed"


def test_a_demo_visitor_prepares_then_confirms_a_service_credit(db_path):
    visitor = demo_visitor(db_path)
    body, proposal = prepare(visitor, CREDIT)
    assert proposal["action_type"] == "issue_service_credit"
    # The amount is the policy engine's, from the LumenWorks agreement.
    assert proposal["parameters"]["amount"] == "300.00"
    assert proposal["parameters"]["requires_manager_approval"] == "false"

    response = confirm(visitor, body, proposal)

    assert response.status_code == 200, response.text
    assert response.json()["action_status"] == "executed"


def test_a_confirmed_action_cannot_be_replayed(db_path):
    visitor = demo_visitor(db_path)
    body, proposal = prepare(visitor, ESCALATE)
    assert confirm(visitor, body, proposal).status_code == 200

    assert confirm(visitor, body, proposal).status_code == 409


def test_a_changed_fingerprint_is_not_executed(db_path):
    visitor = demo_visitor(db_path)
    body, proposal = prepare(visitor, ESCALATE)

    response = confirm(visitor, body, proposal, expected_fingerprint="0" * 32)

    assert response.status_code >= 400
    assert action_status(db_path, proposal["action_id"]) == "pending_confirmation"


def test_the_manager_threshold_still_refuses_the_demo_identity(db_path, monkeypatch):
    """Operations executes ordinary actions. A credit over the SOP threshold
    still needs manager authority, which the demo identity does not have."""
    from test_service_credit_actions import raise_credit_above_threshold

    raise_credit_above_threshold(monkeypatch)
    visitor = demo_visitor(db_path)
    body, proposal = prepare(visitor, CREDIT)
    assert proposal["parameters"]["requires_manager_approval"] == "true"

    response = confirm(visitor, body, proposal)

    assert response.status_code == 403
    assert action_status(db_path, proposal["action_id"]) == "pending_confirmation"


def test_a_support_member_still_cannot_confirm(db_path):
    """Support lost the one-click button and nothing else, and gained nothing."""
    settings = settings_for(db_path)
    ensure_demo_environment(settings)
    support = cold_client(settings)
    assert support.post(
        "/api/auth/login",
        json={"email": "support@demo.astrion.example", "password": settings.demo_password},
    ).status_code == 200
    body, proposal = prepare(support, ESCALATE)

    response = confirm(support, body, proposal)

    assert response.status_code == 403
    assert action_status(db_path, proposal["action_id"]) == "pending_confirmation"


def test_another_workspace_cannot_confirm_a_demo_action(db_path):
    from app.backend.auth import repository as repo
    from app.backend.auth import workspaces as workspace_service
    from app.backend.auth.passwords import hash_password

    visitor = demo_visitor(db_path)
    body, proposal = prepare(visitor, ESCALATE)

    conn = get_connection(db_path)
    try:
        outsider = repo.create_user(
            conn,
            email="owner@elsewhere.example",
            display_name="Elsewhere",
            password_hash=hash_password("elsewhere-password"),
            email_verified=True,
        )
        workspace_service.create_workspace(
            conn, owner_user_id=outsider.user_id, name="Elsewhere", account_ids=[]
        )
    finally:
        conn.close()
    other = cold_client(settings_for(db_path))
    assert other.post(
        "/api/auth/login",
        json={"email": "owner@elsewhere.example", "password": "elsewhere-password"},
    ).status_code == 200

    response = confirm(other, body, proposal)

    # Not 403: another workspace's action does not exist, for this caller.
    assert response.status_code == 404
    assert action_status(db_path, proposal["action_id"]) == "pending_confirmation"


def test_the_confirmation_lands_in_the_audit_trail_the_visitor_can_read(db_path):
    visitor = demo_visitor(db_path)
    body, proposal = prepare(visitor, ESCALATE)
    assert confirm(visitor, body, proposal).status_code == 200

    audit = visitor.get("/api/auth/audit")

    assert audit.status_code == 200
    trail = audit.json()
    assert trail["chain_intact"] is True
    assert any(event["event_type"] == "action.executed" for event in trail["events"])
