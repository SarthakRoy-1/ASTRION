"""Tests for the document API endpoints."""

import pytest

from conftest import SUPPORT_AGENT, SUPPORT_MANAGER, CUSTOMER_LUMENWORKS, CUSTOMER_NORTHSTAR


def test_list_documents_requires_auth(client):
    response = client.get("/api/documents")
    assert response.status_code == 401


def test_list_documents_for_support_manager(client):
    response = client.get("/api/documents", headers={"X-Astrion-User": SUPPORT_MANAGER})
    assert response.status_code == 200
    data = response.json()
    assert "documents" in data
    # Manager sees all ingested documents
    assert len(data["documents"]) > 0


def test_list_documents_for_customer(client):
    response = client.get("/api/documents", headers={"X-Astrion-User": CUSTOMER_NORTHSTAR})
    assert response.status_code == 200
    data = response.json()
    assert "documents" in data
    
    # Customer should only see their own and un-scoped documents
    for doc in data["documents"]:
        assert doc["account_id"] in (None, "ACCT-NORTHSTAR", "ACCT-001", "ACCT-A")


def test_get_document(client):
    # First get list to find an ID
    list_resp = client.get("/api/documents", headers={"X-Astrion-User": SUPPORT_MANAGER})
    doc_id = list_resp.json()["documents"][0]["document_id"]

    response = client.get(f"/api/documents/{doc_id}", headers={"X-Astrion-User": SUPPORT_MANAGER})
    assert response.status_code == 200
    assert response.json()["document_id"] == doc_id


def test_get_document_chunks(client):
    list_resp = client.get("/api/documents", headers={"X-Astrion-User": SUPPORT_MANAGER})
    doc_id = list_resp.json()["documents"][0]["document_id"]

    response = client.get(f"/api/documents/{doc_id}/chunks", headers={"X-Astrion-User": SUPPORT_MANAGER})
    assert response.status_code == 200
    assert "chunks" in response.json()
    assert len(response.json()["chunks"]) > 0


def test_get_ingestion_status(client):
    response = client.get("/api/documents/ingestion-status", headers={"X-Astrion-User": SUPPORT_MANAGER})
    assert response.status_code == 200
    assert "status" in response.json()


# ===========================================================================
# Managing documents
#
# The `documents` table is shared by every workspace, so the questions here
# are tenant questions: who may change what every workspace's agent reads,
# and whose documents one workspace can reach. Everything goes through real
# sessions, because the demo identity header has no permission set to check.
# ===========================================================================

PASSWORD = "document-tests-password"
CURRENT_POLICY = "01_support_policy_v3_current"


@pytest.fixture
def uploads_dir(tmp_path):
    """A private uploads directory. Nothing a test uploads lands in the repo."""
    return tmp_path / "uploads"


@pytest.fixture
def tenants(full_db):
    """Two workspaces over disjoint accounts, each with an owner.

    ACCT-001 and ACCT-002 are the two accounts with signed agreements in the
    supplied pack, which is what makes cross-account attempts meaningful.
    """
    from app.backend.auth import repository as repo
    from app.backend.auth import workspaces as workspace_service
    from app.backend.auth.passwords import hash_password
    from app.backend.services.database import get_connection, initialize_schema

    conn = get_connection(full_db)
    initialize_schema(conn)
    try:
        emails = {}
        for tag, account in (("a", "ACCT-001"), ("b", "ACCT-002")):
            user = repo.create_user(
                conn,
                email=f"owner-{tag}@documents.example",
                display_name=f"Owner {tag}",
                password_hash=hash_password(PASSWORD),
                email_verified=True,
            )
            workspace_service.create_workspace(
                conn, owner_user_id=user.user_id, name=f"Tenant {tag}", account_ids=[account]
            )
            emails[tag] = user.email
        return emails
    finally:
        conn.close()


@pytest.fixture
def session_client_for(full_db, uploads_dir, tenants):
    from fastapi.testclient import TestClient

    from app.backend.api.app import create_app
    from app.backend.core.config import AuthMode, Settings

    settings = Settings(
        database_path=full_db,
        uploads_dir=uploads_dir,
        auth_mode=AuthMode.SESSION,
        cors_allow_origins=(),
        session_cookie_secure=False,
        rate_limit_enabled=False,
    )

    def make(tag: str) -> TestClient:
        client = TestClient(create_app(settings))
        response = client.post(
            "/api/auth/login", json={"email": tenants[tag], "password": PASSWORD}
        )
        assert response.status_code == 200, response.text
        return client

    return make


def agreement_pdf(path, *, account: str | None, title: str, status: str = "ACTIVE"):
    """A real PDF in the supplied pack's own layout, so extraction is exercised."""
    from conftest import write_pdf

    preamble = [(title, 24, True)]
    if account is not None:
        preamble.append((f"Account: {account}", 11, True))
    preamble.append((f"Status: {status}", 11, True))
    write_pdf(
        path,
        [
            preamble
            + [
                ("1. Cancellation terms", 18, True),
                ("Every BOOKED shipment may be cancelled with no fee.", 11, False),
            ]
        ],
    )
    return path


def upload(client, path):
    with open(path, "rb") as handle:
        return client.post(
            "/api/documents/upload",
            files={"file": (path.name, handle, "application/pdf")},
        )


def ids(client):
    return {d["document_id"] for d in client.get("/api/documents").json()["documents"]}


def test_a_workspace_uploads_reads_and_deletes_its_own_agreement(
    session_client_for, tmp_path, uploads_dir
):
    owner_a = session_client_for("a")
    pdf = agreement_pdf(
        tmp_path / "Own_Agreement.pdf", account="ACCT-001", title="ParcelPilot - Own Service Agreement"
    )

    response = upload(owner_a, pdf)
    assert response.status_code == 200, response.text

    uploaded = [d for d in ids(owner_a) if d.endswith("own_agreement")]
    assert len(uploaded) == 1
    stored = list(uploads_dir.glob("*.pdf"))
    assert len(stored) == 1

    assert owner_a.delete(f"/api/documents/{uploaded[0]}").status_code == 200
    assert owner_a.get(f"/api/documents/{uploaded[0]}").status_code == 404
    # The stored file goes with the record.
    assert list(uploads_dir.glob("*.pdf")) == []


def test_another_workspace_can_neither_see_nor_delete_that_upload(session_client_for, tmp_path):
    owner_a, owner_b = session_client_for("a"), session_client_for("b")
    pdf = agreement_pdf(
        tmp_path / "Private_Agreement.pdf", account="ACCT-001", title="ParcelPilot - Private Service Agreement"
    )
    assert upload(owner_a, pdf).status_code == 200
    document_id = next(d for d in ids(owner_a) if d.endswith("private_agreement"))

    assert document_id not in ids(owner_b)
    # 404, not 403: a document outside your scope does not exist, for you.
    assert owner_b.get(f"/api/documents/{document_id}").status_code == 404
    assert owner_b.delete(f"/api/documents/{document_id}").status_code == 404
    assert document_id in ids(owner_a)


def test_an_upload_for_another_workspaces_account_is_refused(session_client_for, tmp_path, uploads_dir):
    owner_a = session_client_for("a")
    pdf = agreement_pdf(
        tmp_path / "Hostile_Agreement.pdf", account="ACCT-002", title="ParcelPilot - Hostile Service Agreement"
    )

    response = upload(owner_a, pdf)

    assert response.status_code == 403
    assert list(uploads_dir.glob("*.pdf")) == []


def test_a_general_document_uploaded_by_a_workspace_is_that_workspaces_alone(
    session_client_for, tmp_path, uploads_dir
):
    """The attack the old rule guarded against: a CURRENT support policy for everyone.

    A document is owned by the workspace that uploaded it. A general one (it names
    no account) is now allowed -- it is the workspace's own policy -- but it is
    stored with its owner, so it reaches that workspace's retrieval and nobody
    else's. It can never become a system document.
    """
    owner_a, owner_b = session_client_for("a"), session_client_for("b")
    before, before_a = ids(owner_b), ids(owner_a)
    pdf = agreement_pdf(
        tmp_path / "Support_Policy_v4.pdf", account=None, title="ParcelPilot Support Policy v4", status="CURRENT"
    )

    response = upload(owner_a, pdf)

    assert response.status_code == 200, response.text
    assert ids(owner_b) == before  # nothing changed for anyone else
    new = ids(owner_a) - before_a
    assert len(new) == 1
    (document_id,) = new
    assert owner_b.get(f"/api/documents/{document_id}").status_code == 404
    assert owner_a.get(f"/api/documents/{document_id}").json()["is_system_document"] is False
    assert owner_a.get(f"/api/documents/{CURRENT_POLICY}").json()["is_system_document"] is True


@pytest.mark.parametrize(
    "document_id",
    [
        CURRENT_POLICY,
        "02_support_policy_v2_deprecated",
        "03_cancellation_and_service_credit_sop_v4",
        "04_product_operations_guide_and_known_issues",
    ],
)
def test_system_documents_cannot_be_deleted_by_any_workspace(session_client_for, document_id):
    """The general documents in the supplied pack belong to no workspace: they are
    the platform knowledge every answer rests on, and none may remove them."""
    owner_a, owner_b = session_client_for("a"), session_client_for("b")

    response = owner_a.delete(f"/api/documents/{document_id}")

    assert response.status_code == 403
    assert owner_b.get(f"/api/documents/{CURRENT_POLICY}").status_code == 200


def test_a_workspace_may_delete_its_own_agreement_but_not_anothers(session_client_for):
    """The customer agreements in the supplied pack belong to the workspace that has
    those customers, so they are that workspace's to manage."""
    owner_a, owner_b = session_client_for("a"), session_client_for("b")
    document = "05_northstar_logistics_enterprise_agreement"

    assert owner_b.delete(f"/api/documents/{document}").status_code == 404
    assert owner_a.delete(f"/api/documents/{document}").status_code == 200
    assert owner_a.get(f"/api/documents/{document}").status_code == 404


@pytest.mark.parametrize("persona", [CUSTOMER_NORTHSTAR, SUPPORT_AGENT, SUPPORT_MANAGER])
def test_the_identity_header_cannot_manage_documents(client, persona):
    """It carries no permission set, so skipping the check would admit everyone."""
    headers = {"X-Astrion-User": persona}

    assert client.delete(f"/api/documents/{CURRENT_POLICY}", headers=headers).status_code == 403
    assert client.post("/api/documents/reindex", headers=headers).status_code == 403
    assert client.get(f"/api/documents/{CURRENT_POLICY}", headers=headers).status_code in (200, 404)
    assert (
        client.get(f"/api/documents/{CURRENT_POLICY}", headers={"X-Astrion-User": SUPPORT_MANAGER}).status_code
        == 200
    )


def test_a_role_without_manage_documents_cannot_upload(full_db, tenants, uploads_dir, tmp_path):
    from fastapi.testclient import TestClient

    from app.backend.api.app import create_app
    from app.backend.auth import repository as repo
    from app.backend.auth.passwords import hash_password
    from app.backend.auth.permissions import OrgRole
    from app.backend.core.config import AuthMode, Settings
    from app.backend.services.database import get_connection

    conn = get_connection(full_db)
    try:
        org_id = repo.org_owning_account(conn, "ACCT-001")
        member = repo.create_user(
            conn,
            email="support@documents.example",
            display_name="Support",
            password_hash=hash_password(PASSWORD),
            email_verified=True,
        )
        repo.add_member(conn, org_id=org_id, user_id=member.user_id, role=OrgRole.SUPPORT)
    finally:
        conn.close()

    client = TestClient(
        create_app(
            Settings(
                database_path=full_db,
                uploads_dir=uploads_dir,
                auth_mode=AuthMode.SESSION,
                cors_allow_origins=(),
                session_cookie_secure=False,
                rate_limit_enabled=False,
            )
        )
    )
    assert client.post(
        "/api/auth/login", json={"email": "support@documents.example", "password": PASSWORD}
    ).status_code == 200
    pdf = agreement_pdf(tmp_path / "Support_Try.pdf", account="ACCT-001", title="ParcelPilot - Try Service Agreement")

    assert upload(client, pdf).status_code == 403
    assert list(uploads_dir.glob("*.pdf")) == []


def test_an_unreadable_upload_is_a_clean_400_that_names_no_server_path(session_client_for, tmp_path):
    owner_a = session_client_for("a")
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4\n% not actually a pdf body")

    response = upload(owner_a, broken)

    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert str(tmp_path) not in message
    assert "uploads" not in message.lower()
    assert ":\\" not in message and "/tmp" not in message


def test_a_document_without_a_status_explains_itself(session_client_for, tmp_path):
    """Validation failures describe the document, and are worth reading."""
    from conftest import write_pdf

    owner_a = session_client_for("a")
    pdf = tmp_path / "No_Status.pdf"
    write_pdf(pdf, [[("ParcelPilot - Nostatus Service Agreement", 24, True), ("Account: ACCT-001", 11, True)]])

    response = upload(owner_a, pdf)

    assert response.status_code == 400
    assert "Status" in response.json()["error"]["message"]


def test_reindex_touches_only_the_callers_workspace(session_client_for, tmp_path):
    owner_a, owner_b = session_client_for("a"), session_client_for("b")
    pdf_a = agreement_pdf(tmp_path / "Reindex_A.pdf", account="ACCT-001", title="ParcelPilot - Reindex A Service Agreement")
    pdf_b = agreement_pdf(tmp_path / "Reindex_B.pdf", account="ACCT-002", title="ParcelPilot - Reindex B Service Agreement")
    assert upload(owner_a, pdf_a).status_code == 200
    assert upload(owner_b, pdf_b).status_code == 200

    response = owner_a.post("/api/documents/reindex")

    assert response.status_code == 200
    assert response.json()["counts"]["documents"] == 1
    assert any(d.endswith("reindex_b") for d in ids(owner_b))
    assert any(d.endswith("reindex_a") for d in ids(owner_a))
