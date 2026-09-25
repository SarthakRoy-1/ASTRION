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


def stored_keys(uploads_dir) -> list[str]:
    """Every object in the document store the tests configure (a local directory)."""
    from app.backend.storage import LocalDocumentStore

    return [o.key for o in LocalDocumentStore(uploads_dir).list()]


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
    stored = stored_keys(uploads_dir)
    assert len(stored) == 1
    assert stored[0].startswith("workspaces/ORG-")  # owned, by its key
    assert stored[0].endswith(".pdf")

    assert owner_a.delete(f"/api/documents/{uploaded[0]}").status_code == 200
    assert owner_a.get(f"/api/documents/{uploaded[0]}").status_code == 404
    # The stored file goes with the record.
    assert stored_keys(uploads_dir) == []


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
    assert stored_keys(uploads_dir) == []


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
    assert stored_keys(uploads_dir) == []


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


# ===========================================================================
# Size limits, and where the files go
# ===========================================================================


def big_agreement_pdf(path, *, account: str, padding_bytes: int):
    """A valid agreement whose file is deliberately larger than an ordinary request."""
    import os

    import pymupdf

    from conftest import valid_agreement_pages, write_pdf

    write_pdf(path, valid_agreement_pages(title="ParcelPilot - Bulky Service Agreement",
                                          account_line=f"Account: {account}"))
    doc = pymupdf.open(str(path))
    doc.embfile_add("attachment.bin", os.urandom(padding_bytes))
    doc.saveIncr()
    doc.close()
    return path


def test_a_document_larger_than_an_ordinary_request_can_be_uploaded(session_client_for, tmp_path):
    """The global request cap is 256 KB; a real policy PDF is not a chat message."""
    owner_a = session_client_for("a")
    pdf = big_agreement_pdf(tmp_path / "Bulky_Agreement.pdf", account="ACCT-001", padding_bytes=600_000)
    assert pdf.stat().st_size > 512 * 1024

    response = upload(owner_a, pdf)

    assert response.status_code == 200, response.text
    assert any(d.endswith("bulky_agreement") for d in ids(owner_a))


def test_a_body_past_the_upload_limit_is_refused_before_it_is_read(session_client_for):
    owner_a = session_client_for("a")
    huge = b"%PDF-1.4\n" + b"0" * (27 * 1024 * 1024)

    response = owner_a.post(
        "/api/documents/upload", files={"file": ("huge.pdf", huge, "application/pdf")}
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_ordinary_routes_keep_the_small_request_limit(session_client_for):
    """Raising the upload limit must not raise it for the endpoints that never needed it."""
    owner_a = session_client_for("a")

    response = owner_a.post(
        "/api/chat", content=b"x" * 300_000, headers={"content-type": "application/json"}
    )

    assert response.status_code == 413


def test_uploads_reach_an_s3_compatible_store_and_leave_when_deleted(
    full_db, tenants, tmp_path
):
    """The whole lifecycle through the configured store, not the local disk."""
    import os
    import uuid

    endpoint = os.environ.get("ASTRION_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("set ASTRION_TEST_S3_ENDPOINT to run against an S3-compatible server")

    from fastapi.testclient import TestClient

    from app.backend.api.app import create_app
    from app.backend.core.config import AuthMode, Settings
    from app.backend.storage.s3 import S3DocumentStore

    bucket = f"astrion-api-{uuid.uuid4().hex[:10]}"
    settings = Settings(
        database_path=full_db,
        auth_mode=AuthMode.SESSION,
        cors_allow_origins=(),
        session_cookie_secure=False,
        rate_limit_enabled=False,
        storage_backend="s3",
        storage_bucket=bucket,
        storage_endpoint_url=endpoint,
        storage_region="us-east-1",
        storage_access_key_id="test-key",
        storage_secret_access_key="test-secret",
        storage_key_prefix="api-tests",
    )
    app = create_app(settings)
    app.state.store.ensure_bucket()
    client = TestClient(app)
    assert client.post(
        "/api/auth/login", json={"email": tenants["a"], "password": PASSWORD}
    ).status_code == 200

    pdf = agreement_pdf(tmp_path / "S3_Agreement.pdf", account="ACCT-001", title="ParcelPilot - S3 Service Agreement")
    assert upload(client, pdf).status_code == 200
    document_id = next(d for d in ids(client) if d.endswith("s3_agreement"))

    objects = [o.key for o in app.state.store.list("workspaces/")]
    assert len(objects) == 1 and objects[0].endswith(".pdf")

    # Reindex reads the original back from the store, checked against its checksum.
    reindexed = client.post("/api/documents/reindex")
    assert reindexed.status_code == 200 and reindexed.json()["counts"]["documents"] >= 1
    assert len([o.key for o in app.state.store.list("workspaces/")]) == 1

    assert client.delete(f"/api/documents/{document_id}").status_code == 200
    assert [o.key for o in app.state.store.list("workspaces/")] == []


def test_a_tampered_original_is_skipped_by_reindex_not_trusted(session_client_for, tmp_path, uploads_dir):
    from app.backend.storage import LocalDocumentStore

    owner_a = session_client_for("a")
    pdf = agreement_pdf(tmp_path / "Tamper_Agreement.pdf", account="ACCT-001", title="ParcelPilot - Tamper Service Agreement")
    assert upload(owner_a, pdf).status_code == 200
    store = LocalDocumentStore(uploads_dir)
    (key,) = [o.key for o in store.list("workspaces/")]
    store.put(key, b"%PDF-1.4 not the file that was uploaded")

    reindexed = owner_a.post("/api/documents/reindex")

    assert reindexed.status_code == 200
    assert reindexed.json()["counts"]["skipped"] >= 1
    # The document's extracted text is what it was; the swapped bytes were never parsed into it.
    document_id = next(d for d in ids(owner_a) if d.endswith("tamper_agreement"))
    assert owner_a.get(f"/api/documents/{document_id}/chunks").json()["chunks"]


def plain_letter_pdf(path):
    """An ordinary letter: no title, no headings, no Status."""
    from conftest import write_pdf

    write_pdf(
        path,
        [[
            ("Dear Hiring Manager,", 11, False),
            ("I am writing to apply for the Support Engineer role.", 11, False),
            ("Sincerely, Sarthak Roy", 11, False),
        ]],
    )
    return path


def test_an_ordinary_pdf_with_no_title_or_status_is_kept_as_a_reference(
    session_client_for, tmp_path, uploads_dir
):
    """The production failure: 'no document title found' for a cover letter."""
    owner_a, owner_b = session_client_for("a"), session_client_for("b")
    before_a, before_b = ids(owner_a), ids(owner_b)
    pdf = plain_letter_pdf(tmp_path / "Sarthak_Roy_Rippling_Cover_Letter.pdf")

    response = upload(owner_a, pdf)

    assert response.status_code == 200, response.text
    (document_id,) = ids(owner_a) - before_a
    document = owner_a.get(f"/api/documents/{document_id}").json()
    assert document["title"] == "Sarthak Roy Rippling Cover Letter"
    assert document["original_filename"] == "Sarthak_Roy_Rippling_Cover_Letter.pdf"
    assert document["document_type"] == "reference"
    assert document["status"] == "UNSTATED"
    assert document["is_authoritative"] is False
    assert document["is_system_document"] is False
    # Workspace isolation is unchanged: the other workspace sees nothing of it.
    assert ids(owner_b) == before_b
    assert owner_b.get(f"/api/documents/{document_id}").status_code == 404
    assert len(stored_keys(uploads_dir)) == 1


def test_a_reference_survives_reindex_and_can_be_deleted(session_client_for, tmp_path, uploads_dir):
    owner_a = session_client_for("a")
    before = ids(owner_a)
    assert upload(owner_a, plain_letter_pdf(tmp_path / "Cover_Letter.pdf")).status_code == 200
    (document_id,) = ids(owner_a) - before

    reindexed = owner_a.post("/api/documents/reindex")
    assert reindexed.status_code == 200, reindexed.text
    # Exactly the reference was re-read from its stored original (the fixture's
    # seeded agreement has no original, and is skipped as it always was).
    assert reindexed.json()["counts"]["documents"] == 1
    assert owner_a.get(f"/api/documents/{document_id}").json()["title"] == "Cover Letter"
    assert owner_a.get(f"/api/documents/{document_id}").json()["document_type"] == "reference"

    assert owner_a.delete(f"/api/documents/{document_id}").status_code == 200
    assert stored_keys(uploads_dir) == []


def test_a_controlled_looking_document_without_a_status_is_still_refused_by_the_api(
    session_client_for, tmp_path, uploads_dir
):
    """Leniency for a letter is not leniency for a document that claims authority."""
    from conftest import write_pdf

    owner_a = session_client_for("a")
    pdf = tmp_path / "Policy_No_Status.pdf"
    write_pdf(pdf, [[("Acme Support Policy v9", 24, True), ("1. Escalation", 18, True), ("Escalate.", 11, False)]])

    response = upload(owner_a, pdf)

    assert response.status_code == 400
    assert "Status" in response.json()["error"]["message"]
    assert stored_keys(uploads_dir) == []
