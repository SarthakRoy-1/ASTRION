"""A new workspace starts empty, and the product works anyway.

The architecture no longer needs the assessment workbook. A user signs up,
creates a workspace, and finds: no accounts, no orders, no tickets, no documents
of their own -- and the platform's system documents (the support policy, the SOP,
the operations guide) already there for the assistant to cite. Nothing in this
path may depend on a seeded demo, a demo login or the legacy dataset.

Runs on either engine.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.backend.api.app import create_app
from app.backend.core.config import AuthMode, Settings
from app.backend.services.database import get_connection, initialize_schema
from conftest import SOURCE_DIR
from scripts import ingest_documents

PASSWORD = "correct-horse-battery-staple"
SYSTEM_DOCUMENTS = 4  # the general documents in the supplied pack


@pytest.fixture
def platform_db(tmp_path):
    """A database holding only what a production deployment loads: system documents."""
    path = tmp_path / "platform.db"
    result = ingest_documents.ingest(source_dir=SOURCE_DIR, db_path=path, org_id=None)
    assert result["counts"]["documents"] == SYSTEM_DOCUMENTS
    return path


def settings_for(path) -> Settings:
    return Settings(
        database_path=path,
        auth_mode=AuthMode.SESSION,
        cors_allow_origins=(),
        session_cookie_secure=False,
        rate_limit_enabled=False,
        require_verified_email=False,
        demo_login_enabled=False,
    )


def signed_up_owner(path, email="founder@newco.test") -> TestClient:
    client = TestClient(create_app(settings_for(path)))
    assert client.post(
        "/api/auth/register",
        json={"email": email, "password": PASSWORD, "display_name": "Founder"},
    ).status_code == 200
    assert client.post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200
    return client


def test_loading_only_the_system_documents_creates_no_workspace_and_no_records(platform_db):
    conn = get_connection(platform_db)
    initialize_schema(conn)
    try:
        assert conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM documents WHERE org_id IS NOT NULL").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM documents WHERE org_id IS NULL").fetchone()[0] == SYSTEM_DOCUMENTS
    finally:
        conn.close()


def test_loading_system_documents_twice_changes_nothing(platform_db):
    ingest_documents.ingest(source_dir=SOURCE_DIR, db_path=platform_db, org_id=None)
    conn = get_connection(platform_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == SYSTEM_DOCUMENTS
    finally:
        conn.close()


def test_health_is_ready_once_the_system_documents_are_loaded(platform_db):
    client = TestClient(create_app(settings_for(platform_db)))
    body = client.get("/health").json()
    assert body["database_ready"] is True and body["status"] == "ok"
    assert body["documents_indexed"] == SYSTEM_DOCUMENTS
    assert body["demo_login_enabled"] is False


def test_a_new_user_can_create_a_workspace_that_starts_empty(platform_db):
    client = signed_up_owner(platform_db)
    created = client.post(
        "/api/workspaces",
        json={
            "name": "New Co",
            "workspace_password": "a-shared-team-secret",
            "confirm_workspace_password": "a-shared-team-secret",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["role"] == "owner"

    # No accounts, no operational signals, and nothing of its own to read.
    me = client.get("/api/auth/me").json()
    assert me["account_scope"] == []
    signals = client.get("/api/operations/signals")
    assert signals.status_code == 200 and signals.json()["signals"] == []

    # The platform's documents are there; there is nothing else.
    documents = client.get("/api/documents").json()["documents"]
    assert len(documents) == SYSTEM_DOCUMENTS
    assert all(d["is_system_document"] for d in documents)


def test_the_assistant_answers_from_the_system_documents_in_an_empty_workspace(platform_db):
    client = signed_up_owner(platform_db)
    client.post(
        "/api/workspaces",
        json={
            "name": "New Co",
            "workspace_password": "a-shared-team-secret",
            "confirm_workspace_password": "a-shared-team-secret",
        },
    )

    # A record question: there is no such order here, and it says so rather than failing.
    record = client.post("/api/chat", json={"message": "What is the status of ORD-1001?"})
    assert record.status_code == 200, record.text
    assert "ORD-1001" in record.text

    # A policy question: answerable from platform knowledge alone.
    policy = client.post(
        "/api/chat", json={"message": "How quickly must a P1 ticket receive a first response?"}
    )
    assert policy.status_code == 200, policy.text


def test_two_new_workspaces_start_with_the_same_system_documents_and_nothing_shared_beyond(platform_db):
    first = signed_up_owner(platform_db, "one@newco.test")
    second = signed_up_owner(platform_db, "two@newco.test")
    for client, name in ((first, "One"), (second, "Two")):
        assert client.post(
            "/api/workspaces",
            json={
                "name": name,
                "workspace_password": "a-shared-team-secret",
                "confirm_workspace_password": "a-shared-team-secret",
            },
        ).status_code == 201
    assert {d["document_id"] for d in first.get("/api/documents").json()["documents"]} == {
        d["document_id"] for d in second.get("/api/documents").json()["documents"]
    }


def test_system_documents_are_kept_as_objects_under_system_and_loading_is_idempotent(tmp_path):
    from app.backend.storage import LocalDocumentStore

    path = tmp_path / "sys.db"
    store = LocalDocumentStore(tmp_path / "objects")
    for _ in range(2):
        result = ingest_documents.ingest(source_dir=SOURCE_DIR, db_path=path, org_id=None, store=store)
        assert result["counts"]["objects_stored"] == SYSTEM_DOCUMENTS

    keys = sorted(o.key for o in store.list(""))
    assert len(keys) == SYSTEM_DOCUMENTS  # the same bytes land at the same keys
    assert all(k.startswith("system/documents/") and k.endswith(".pdf") for k in keys)

    conn = get_connection(path)
    try:
        recorded = sorted(r[0] for r in conn.execute("SELECT storage_key FROM documents"))
        assert recorded == keys
    finally:
        conn.close()
