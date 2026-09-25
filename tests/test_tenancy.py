"""The workspace is the tenant boundary: what that means, stated as behaviour.

Two workspaces are built to be *as similar as possible*: they hold accounts,
orders and tickets with the **same ids** (an `ACCT-1` in each, an `ORD-1`, a
`TKT-1`) and different contents. That is the case the old global-account model
could not represent and the one an isolation bug would hide in: if any query
filtered on the id alone, one workspace would read the other's row.

They run on either engine (see tests/pg_backend.py).
"""

from __future__ import annotations

import pytest

from app.backend.auth import repository as repo
from app.backend.db import IntegrityError
from app.backend.models.actions import ActionType
from app.backend.policies.base import load_evaluation_context
from app.backend.retrieval.extraction import DocumentIngestionError
from app.backend.retrieval.search import search_documents
from app.backend.services import documents as docs
from app.backend.services import operations as ops
from app.backend.services import records
from app.backend.services.actions import (
    ActionNotFound,
    execute_action,
    get_action,
    get_order_service_credits,
    get_ticket_escalations,
    list_pending_actions,
    prepare_action,
)
from app.backend.services.database import get_connection, initialize_schema
from app.backend.tenancy import LEGACY_ORG_ID, Scope

A, B = "ORG-alpha", "ORG-beta"


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "tenancy.db")
    initialize_schema(connection)
    yield connection
    connection.close()


def add_org(conn, org_id: str) -> None:
    conn.execute(
        "INSERT INTO organizations (org_id, name, slug, created_at_utc) VALUES (?,?,?,?)",
        (org_id, org_id, org_id.lower(), "t"),
    )
    conn.commit()


def add_customer(conn, org: str, name: str, fee: float, subject: str) -> None:
    """ACCT-1 / ORD-1 / TKT-1 in `org`, with contents that differ per workspace."""
    conn.execute(
        "INSERT INTO accounts (org_id, account_id, account_name) VALUES (?,?,?)",
        (org, "ACCT-1", name),
    )
    conn.execute(
        "INSERT INTO orders (org_id, order_id, account_id, carrier, status, shipment_fee_inr) "
        "VALUES (?, 'ORD-1', 'ACCT-1', 'Carrier', 'BOOKED', ?)",
        (org, fee),
    )
    conn.execute(
        "INSERT INTO tickets (org_id, ticket_id, account_id, subject, status, created_at) "
        "VALUES (?, 'TKT-1', 'ACCT-1', ?, 'open', '2026-01-01T00:00:00+00:00')",
        (org, subject),
    )
    conn.commit()


@pytest.fixture
def world(conn):
    add_org(conn, A)
    add_org(conn, B)
    add_customer(conn, A, "Alpha Customer", 100.0, "alpha problem")
    add_customer(conn, B, "Beta Customer", 999.0, "beta problem")
    # One account that exists only in beta, to probe ids across the boundary.
    conn.execute("INSERT INTO accounts (org_id, account_id, account_name) VALUES (?,?,?)", (B, "ACCT-BETA-ONLY", "Beta Only"))
    conn.execute(
        "INSERT INTO orders (org_id, order_id, account_id) VALUES (?, 'ORD-BETA-ONLY', 'ACCT-BETA-ONLY')",
        (B,),
    )
    conn.commit()
    return conn


def sa(org=A, accounts=None) -> Scope:
    return Scope.of(org, accounts)


# --- accounts, orders, tickets ----------------------------------------------------


def test_the_same_account_id_in_two_workspaces_is_two_accounts(world):
    assert records.get_account(world, "ACCT-1", scope=sa(A)).account_name == "Alpha Customer"
    assert records.get_account(world, "ACCT-1", scope=sa(B)).account_name == "Beta Customer"


def test_the_same_order_and_ticket_ids_never_cross(world):
    assert records.get_order(world, "ORD-1", scope=sa(A)).shipment_fee_inr == 100.0
    assert records.get_order(world, "ORD-1", scope=sa(B)).shipment_fee_inr == 999.0
    assert records.get_ticket(world, "TKT-1", scope=sa(A)).subject == "alpha problem"
    assert records.get_ticket(world, "TKT-1", scope=sa(B)).subject == "beta problem"
    assert [o.shipment_fee_inr for o in records.get_account_orders(world, "ACCT-1", scope=sa(A))] == [100.0]
    assert [t.subject for t in records.get_account_tickets(world, "ACCT-1", scope=sa(B))] == ["beta problem"]


def test_an_id_that_exists_only_in_another_workspace_is_absent(world):
    """Cross-workspace id manipulation: asking for someone else's record by id."""
    assert records.get_account(world, "ACCT-BETA-ONLY", scope=sa(A)) is None
    assert records.get_order(world, "ORD-BETA-ONLY", scope=sa(A)) is None
    assert records.get_account_orders(world, "ACCT-BETA-ONLY", scope=sa(A)) == []
    # ...and present, of course, for its owner.
    assert records.get_order(world, "ORD-BETA-ONLY", scope=sa(B)) is not None


def test_account_narrowing_only_narrows_within_the_workspace(world):
    assert records.get_account(world, "ACCT-1", scope=sa(A, {"ACCT-1"})) is not None
    assert records.get_account(world, "ACCT-1", scope=sa(A, {"ACCT-OTHER"})) is None
    assert records.get_account(world, "ACCT-1", scope=sa(A, set())) is None
    # Naming the other workspace's account in a narrowing grants nothing.
    assert records.get_account(world, "ACCT-BETA-ONLY", scope=sa(A, {"ACCT-BETA-ONLY"})) is None


def test_a_scope_with_no_workspace_reaches_nothing(world):
    none = Scope(None)
    assert records.get_account(world, "ACCT-1", scope=none) is None
    assert records.get_order(world, "ORD-1", scope=none) is None
    assert ops.list_tickets(world, scope=none) == []
    assert ops.list_orders(world, scope=none) == []
    assert ops.account_names(world, scope=none) == {}
    assert records.get_all_account_ids(world, none) == []


def test_aggregates_count_only_the_callers_workspace(world):
    assert {t.subject for t in ops.list_tickets(world, scope=sa(A))} == {"alpha problem"}
    assert {t.subject for t in ops.list_tickets(world, scope=sa(B))} == {"beta problem"}
    assert ops.account_names(world, scope=sa(A)) == {"ACCT-1": "Alpha Customer"}
    assert ops.account_names(world, scope=sa(B)) == {"ACCT-1": "Beta Customer", "ACCT-BETA-ONLY": "Beta Only"}
    assert ops.count_tickets_by_account(world, scope=sa(A)) == {"ACCT-1": 1}
    assert ops.count_orders_by_carrier(world, scope=sa(A))["Carrier"]["orders"] == 1


def test_an_order_cannot_be_stored_for_an_account_its_workspace_lacks(world):
    with pytest.raises(IntegrityError):
        world.execute(
            "INSERT INTO orders (org_id, order_id, account_id) VALUES (?, 'ORD-X', 'ACCT-BETA-ONLY')",
            (A,),
        )


# --- documents ---------------------------------------------------------------------


def add_document(conn, document_id: str, org: str | None, account: str | None, text: str) -> None:
    if conn.execute("SELECT 1 FROM document_ingestion_runs").fetchone() is None:
        conn.execute(
            "INSERT INTO document_ingestion_runs (started_at_utc, source_dir, ingestion_script_version) "
            "VALUES ('t', 'd', 'v')"
        )
    conn.execute(
        "INSERT INTO documents (document_id, org_id, source_file, source_sha256, title, "
        "document_type, status, status_raw, is_current, is_deprecated, is_authoritative, "
        "authority_tier, account_id, page_count, ingestion_run_id) "
        "VALUES (?, ?, ?, 'h', ?, 'support_policy', 'CURRENT', 'CURRENT', 1, 0, 1, 2, ?, 1, 1)",
        (document_id, org, f"{document_id}.pdf", document_id, account),
    )
    conn.execute(
        "INSERT INTO document_chunks (chunk_id, document_id, chunk_ordinal, page_number, topic, "
        "text, char_count, word_count, page_char_start, page_char_end) "
        "VALUES (?, ?, 0, 1, 'general', ?, ?, 1, 0, ?)",
        (f"{document_id}#c0", document_id, text, len(text), len(text)),
    )
    conn.commit()


@pytest.fixture
def library(world):
    add_document(world, "system-policy", None, None, "platform refund policy applies everywhere")
    add_document(world, "alpha-general", A, None, "alpha internal procedure zebra")
    add_document(world, "alpha-agreement", A, "ACCT-1", "alpha customer agreement zebra terms")
    add_document(world, "beta-general", B, None, "beta internal procedure zebra")
    add_document(world, "beta-agreement", B, "ACCT-1", "beta customer agreement zebra terms")
    return world


def visible(conn, scope: Scope) -> set[str]:
    return {d.document_id for d in docs.list_documents(conn, scope=scope)}


def test_a_workspace_sees_the_system_documents_and_its_own_and_no_others(library):
    assert visible(library, sa(A)) == {"system-policy", "alpha-general", "alpha-agreement"}
    assert visible(library, sa(B)) == {"system-policy", "beta-general", "beta-agreement"}


def test_a_scope_with_no_workspace_sees_only_system_documents(library):
    assert visible(library, Scope(None)) == {"system-policy"}


def test_account_narrowing_hides_customer_documents_not_general_ones(library):
    assert visible(library, sa(A, set())) == {"system-policy", "alpha-general"}
    assert visible(library, sa(A, {"ACCT-1"})) == {"system-policy", "alpha-general", "alpha-agreement"}


def test_another_workspaces_document_cannot_be_read_by_id(library):
    assert docs.get_document(library, "beta-general", scope=sa(A)) is None
    assert docs.get_document(library, "beta-agreement", scope=sa(A)) is None
    assert docs.get_document_chunks(library, "beta-general", scope=sa(A)) == []
    assert docs.get_evidence_by_chunk_ids(library, ["beta-general#c0"], scope=sa(A)) == []
    # A same-named account in the other workspace is not the same customer.
    assert docs.get_document(library, "beta-agreement", account_id="ACCT-1", scope=sa(A)) is None


def test_search_never_returns_another_workspaces_text(library):
    hits = search_documents(library, "zebra", scope=sa(A))
    assert {h.document_id for h in hits} == {"alpha-general", "alpha-agreement"}
    assert all("beta" not in h.text for h in hits)
    assert {h.document_id for h in search_documents(library, "zebra", scope=sa(B))} == {
        "beta-general",
        "beta-agreement",
    }


def test_a_system_document_is_searchable_by_every_workspace(library):
    for scope in (sa(A), sa(B), Scope(None)):
        assert {h.document_id for h in search_documents(library, "platform refund policy", scope=scope)} == {
            "system-policy"
        }


def test_a_workspace_deletes_its_own_documents_only(library):
    assert docs.delete_document(library, "system-policy", scope=sa(A)) is False  # nobody's to delete
    assert docs.delete_document(library, "beta-general", scope=sa(A)) is False  # someone else's
    assert docs.delete_document(library, "alpha-general", scope=sa(A)) is True
    assert visible(library, sa(A)) == {"system-policy", "alpha-agreement"}
    assert visible(library, sa(B)) == {"system-policy", "beta-general", "beta-agreement"}
    assert docs.delete_document(library, "alpha-agreement", scope=Scope(None)) is False


def test_a_system_document_cannot_name_an_account(library):
    with pytest.raises(IntegrityError):
        add_document(library, "bad-system", None, "ACCT-1", "a system document naming an account")


def test_a_document_id_owned_by_someone_else_is_never_replaced_by_an_upload(library, tmp_path):
    from app.backend.services.document_ingestion import ingest_single_document

    class Fake:
        class document:  # noqa: N801 - mimics ExtractedDocument.document
            document_id = "beta-general"
            source_file = "beta-general.pdf"

    with pytest.raises(DocumentIngestionError, match="another owner"):
        ingest_single_document(library, Fake, str(tmp_path), org_id=A)  # type: ignore[arg-type]
    # Nothing of beta's changed.
    assert docs.get_document(library, "beta-general", scope=sa(B)) is not None


# --- prepared actions --------------------------------------------------------------


def prepare(conn, org: str):
    return prepare_action(
        conn,
        org_id=org,
        action_type=ActionType.CREATE_ESCALATION,
        target_type="ticket",
        target_id="TKT-1",
        parameters={"reason": "customer is waiting", "severity": "high"},
        requested_by="USR-1",
        requested_by_role="operations",
        account_id="ACCT-1",
    )


def test_an_action_belongs_to_the_workspace_that_prepared_it(world):
    action = prepare(world, A)
    assert get_action(world, action.action_id, scope=sa(A)) is not None
    assert get_action(world, action.action_id, scope=sa(B)) is None
    assert [a.action_id for a in list_pending_actions(world, scope=sa(A))] == [action.action_id]
    assert list_pending_actions(world, scope=sa(B)) == []


def test_another_workspace_cannot_execute_an_action_it_did_not_prepare(world):
    action = prepare(world, A)
    with pytest.raises(ActionNotFound):
        execute_action(world, action.action_id, confirmed_by="USR-B", scope=sa(B))
    # And it is still pending, untouched, for its owner.
    assert get_action(world, action.action_id, scope=sa(A)).status.value == "pending_confirmation"


def test_the_effects_of_an_action_are_recorded_and_read_within_its_workspace(world):
    action = prepare(world, A)
    execute_action(world, action.action_id, confirmed_by="USR-A", scope=sa(A))
    # TKT-1 exists in both workspaces; only alpha's has an escalation.
    assert len(get_ticket_escalations(world, "TKT-1", org_id=A)) == 1
    assert get_ticket_escalations(world, "TKT-1", org_id=B) == []
    assert get_ticket_escalations(world, "TKT-1", org_id=None) == []
    assert get_order_service_credits(world, "ORD-1", org_id=A) == []


def test_an_action_cannot_be_prepared_without_a_workspace(world):
    from app.backend.services.actions import ActionError

    with pytest.raises(ActionError):
        prepare(world, "")


# --- the reference clock -----------------------------------------------------------


def test_a_workspace_with_no_snapshot_is_judged_against_the_current_time(world):
    context = load_evaluation_context(world, A)
    assert "current time" in context.reference_time_source
    assert context.currency == "INR"


def test_a_workspace_with_a_snapshot_is_judged_against_it(world):
    world.execute(
        "INSERT INTO dataset_metadata (org_id, dataset_snapshot_raw, dataset_snapshot_at, "
        "dataset_timezone, currency, source_workbook_filename, source_workbook_sha256, "
        "source_sheet_names, ingested_at_utc, ingestion_script_version) "
        "VALUES (?, '2026-08-16 11:00 Asia/Kolkata', '2026-08-16T11:00:00+05:30', 'Asia/Kolkata', "
        "'INR', 'w.xlsx', 'h', '[]', '2026-01-01T00:00:00+00:00', 'v')",
        (A,),
    )
    world.commit()
    assert "dataset snapshot" in load_evaluation_context(world, A).reference_time_source
    assert "current time" in load_evaluation_context(world, B).reference_time_source


# --- moving imported accounts ------------------------------------------------------


def test_moving_an_account_moves_everything_under_it_and_nothing_else(conn):
    add_org(conn, LEGACY_ORG_ID)
    add_org(conn, A)
    add_customer(conn, LEGACY_ORG_ID, "Legacy Customer", 50.0, "legacy problem")
    conn.execute(
        "INSERT INTO accounts (org_id, account_id, account_name) VALUES (?, 'ACCT-STAY', 'Stays')",
        (LEGACY_ORG_ID,),
    )
    conn.commit()
    add_document(conn, "legacy-agreement", LEGACY_ORG_ID, "ACCT-1", "legacy agreement")

    repo.grant_account(conn, org_id=A, account_id="ACCT-1")

    assert records.get_account(conn, "ACCT-1", scope=Scope(A)).account_name == "Legacy Customer"
    assert records.get_order(conn, "ORD-1", scope=Scope(A)) is not None
    assert records.get_ticket(conn, "TKT-1", scope=Scope(A)) is not None
    assert docs.get_document(conn, "legacy-agreement", scope=Scope(A)) is not None
    # Gone from where it was, while the rest of that workspace is untouched.
    assert records.get_account(conn, "ACCT-1", scope=Scope(LEGACY_ORG_ID)) is None
    assert records.get_order(conn, "ORD-1", scope=Scope(LEGACY_ORG_ID)) is None
    assert records.get_account(conn, "ACCT-STAY", scope=Scope(LEGACY_ORG_ID)) is not None


def test_moving_onto_an_existing_account_is_refused_not_merged(conn):
    add_org(conn, LEGACY_ORG_ID)
    add_org(conn, A)
    add_customer(conn, LEGACY_ORG_ID, "Legacy Customer", 50.0, "legacy problem")
    add_customer(conn, A, "Alpha Customer", 100.0, "alpha problem")

    with pytest.raises(repo.AccountAlreadyClaimedError):
        repo.grant_account(conn, org_id=A, account_id="ACCT-1")

    assert records.get_account(conn, "ACCT-1", scope=Scope(A)).account_name == "Alpha Customer"
    assert records.get_account(conn, "ACCT-1", scope=Scope(LEGACY_ORG_ID)).account_name == "Legacy Customer"


# --- the API: a workspace's documents and its members ------------------------------


def test_the_documents_api_lists_only_the_callers_workspace(tmp_path):
    """End to end over HTTP: two signed-in owners, one document each."""
    from fastapi.testclient import TestClient

    from app.backend.api.app import create_app
    from app.backend.auth import workspaces as workspace_service
    from app.backend.auth.passwords import hash_password
    from app.backend.core.config import AuthMode, Settings

    path = tmp_path / "api.db"
    setup = get_connection(path)
    initialize_schema(setup)
    password = "correct-horse-battery-staple"
    orgs = {}
    for key in ("a", "b"):
        user = repo.create_user(
            setup, email=f"{key}@tenancy.test", display_name=key,
            password_hash=hash_password(password), email_verified=True,
        )
        orgs[key] = workspace_service.create_workspace(
            setup, owner_user_id=user.user_id, name=f"Workspace {key}"
        )["org_id"]
    add_document(setup, "system-policy", None, None, "platform policy")
    add_document(setup, "doc-a", orgs["a"], None, "alpha only")
    add_document(setup, "doc-b", orgs["b"], None, "beta only")
    setup.close()

    settings = Settings(
        database_path=path, auth_mode=AuthMode.SESSION, cors_allow_origins=(),
        session_cookie_secure=False, rate_limit_enabled=False,
    )
    seen = {}
    for key in ("a", "b"):
        client = TestClient(create_app(settings))
        assert client.post(
            "/api/auth/login", json={"email": f"{key}@tenancy.test", "password": password}
        ).status_code == 200
        body = client.get("/api/documents").json()["documents"]
        seen[key] = {d["document_id"] for d in body}
        # The response says what is system knowledge, not whose the rest is.
        assert {d["document_id"] for d in body if d["is_system_document"]} == {"system-policy"}
        assert "org_id" not in body[0]
        other = "doc-b" if key == "a" else "doc-a"
        assert client.get(f"/api/documents/{other}").status_code == 404
        assert client.delete(f"/api/documents/{other}").status_code == 404
        assert client.delete("/api/documents/system-policy").status_code == 403
    assert seen == {"a": {"system-policy", "doc-a"}, "b": {"system-policy", "doc-b"}}
