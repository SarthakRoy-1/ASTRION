from datetime import datetime

import pytest

from app.backend.models.records import Account, Order, Ticket
from app.backend.services import records
from app.backend.services.database import get_connection
from conftest import default_sheets, write_workbook
from scripts import ingest_dataset


@pytest.fixture
def conn(tmp_path):
    workbook_path = tmp_path / "data.xlsx"
    write_workbook(workbook_path, default_sheets())
    db_path = tmp_path / "test.db"
    ingest_dataset.ingest(workbook_path=workbook_path, db_path=db_path)

    connection = get_connection(db_path)
    yield connection
    connection.close()


# --- get_account -------------------------------------------------------------


def test_get_account_found(conn):
    account = records.get_account(conn, "ACCT-A")

    assert isinstance(account, Account)
    assert account.account_id == "ACCT-A"
    assert account.account_name == "Alpha Co"
    assert account.premium_support is True


def test_get_account_not_found_returns_none(conn):
    assert records.get_account(conn, "ACCT-DOES-NOT-EXIST") is None


def test_get_account_found_with_null_field_distinguishable_from_not_found(conn):
    account = records.get_account(conn, "ACCT-B")

    assert account is not None
    assert account.contract_file is None  # found, but this field is null
    not_found = records.get_account(conn, "ACCT-ZZZ")
    assert not_found is None
    assert account != not_found


# --- get_order -----------------------------------------------------------------


def test_get_order_found(conn):
    order = records.get_order(conn, "ORD-1")

    assert isinstance(order, Order)
    assert order.order_id == "ORD-1"
    assert order.account_id == "ACCT-A"
    assert isinstance(order.booked_at, datetime)
    assert order.booked_at.utcoffset().total_seconds() == 5.5 * 3600


def test_get_order_not_found_returns_none(conn):
    assert records.get_order(conn, "ORD-999") is None


def test_get_order_null_pickup_actual_at_is_none_not_missing(conn):
    order = records.get_order(conn, "ORD-1")
    assert order is not None
    assert order.pickup_actual_at is None


# --- get_ticket -----------------------------------------------------------------


def test_get_ticket_found(conn):
    ticket = records.get_ticket(conn, "TKT-2")

    assert isinstance(ticket, Ticket)
    assert ticket.historical_resolution == "Told customer X."


def test_get_ticket_not_found_returns_none(conn):
    assert records.get_ticket(conn, "TKT-999") is None


def test_get_ticket_null_historical_resolution(conn):
    ticket = records.get_ticket(conn, "TKT-1")
    assert ticket is not None
    assert ticket.historical_resolution is None


# --- account -> orders / tickets ------------------------------------------------


def test_get_account_orders_returns_only_that_accounts_orders(conn):
    orders = records.get_account_orders(conn, "ACCT-A")

    assert [o.order_id for o in orders] == ["ORD-1"]
    assert all(o.account_id == "ACCT-A" for o in orders)


def test_get_account_orders_unknown_account_returns_empty_list(conn):
    assert records.get_account_orders(conn, "ACCT-ZZZ") == []


def test_get_account_tickets_returns_only_that_accounts_tickets(conn):
    tickets = records.get_account_tickets(conn, "ACCT-B")

    assert [t.ticket_id for t in tickets] == ["TKT-2"]


def test_get_account_tickets_unknown_account_returns_empty_list(conn):
    assert records.get_account_tickets(conn, "ACCT-ZZZ") == []


# --- account scoping hook -------------------------------------------------------


def test_get_account_respects_allowed_account_ids(conn):
    in_scope = records.get_account(conn, "ACCT-A", allowed_account_ids={"ACCT-A"})
    assert in_scope is not None

    out_of_scope = records.get_account(conn, "ACCT-B", allowed_account_ids={"ACCT-A"})
    assert out_of_scope is None


def test_get_order_out_of_scope_is_indistinguishable_from_not_found(conn):
    out_of_scope = records.get_order(conn, "ORD-2", allowed_account_ids={"ACCT-A"})
    not_found = records.get_order(conn, "ORD-999", allowed_account_ids={"ACCT-A"})

    assert out_of_scope is None
    assert not_found is None


def test_get_account_orders_out_of_scope_returns_empty_list(conn):
    assert records.get_account_orders(conn, "ACCT-B", allowed_account_ids={"ACCT-A"}) == []


def test_get_account_tickets_out_of_scope_returns_empty_list(conn):
    assert records.get_account_tickets(conn, "ACCT-B", allowed_account_ids={"ACCT-A"}) == []


def test_no_scope_restriction_by_default(conn):
    # Omitting allowed_account_ids must not silently restrict anything --
    # Phase 5 (auth) has not been implemented, so today's default caller can
    # see all accounts.
    assert records.get_account(conn, "ACCT-B") is not None
    assert len(records.get_account_orders(conn, "ACCT-B")) == 1


# --- dataset metadata ------------------------------------------------------------


def test_get_dataset_metadata(conn):
    meta = records.get_dataset_metadata(conn)

    assert meta is not None
    assert meta.dataset_snapshot_raw == "2026-01-10 09:00 Asia/Kolkata"
    assert meta.dataset_timezone == "Asia/Kolkata"
    assert meta.source_sheet_names == ["README", "accounts", "orders", "tickets"]


def test_get_dataset_metadata_missing_returns_none(tmp_path):
    from app.backend.services.database import initialize_schema

    empty_conn = get_connection(tmp_path / "empty.db")
    try:
        initialize_schema(empty_conn)
        assert records.get_dataset_metadata(empty_conn) is None
    finally:
        empty_conn.close()


# --- provenance --------------------------------------------------------------------


def test_get_source_provenance_for_known_record(conn):
    prov = records.get_source_provenance(conn, "orders", "ORD-1")

    assert prov is not None
    assert prov.source_sheet == "orders"
    assert prov.source_row_number == 2
    assert prov.raw_row["booked_at"] == "2026-01-10 09:00"


def test_get_source_provenance_unknown_returns_none(conn):
    assert records.get_source_provenance(conn, "orders", "ORD-999") is None


# --- SQL injection safety ----------------------------------------------------------


def test_get_account_is_safe_against_injection_style_input(conn):
    malicious_id = "ACCT-A' OR '1'='1"

    result = records.get_account(conn, malicious_id)

    assert result is None  # treated as a literal (non-matching) id, not SQL
    # and the database is unharmed -- real accounts are still there
    assert records.get_account(conn, "ACCT-A") is not None


def test_get_order_is_safe_against_drop_table_payload(conn):
    payload = "ORD-1'; DROP TABLE orders; --"

    result = records.get_order(conn, payload)

    assert result is None
    assert records.get_order(conn, "ORD-1") is not None  # table still exists with data


def test_get_account_orders_is_safe_against_injection_style_input(conn):
    result = records.get_account_orders(conn, "ACCT-A' OR '1'='1")

    assert result == []
