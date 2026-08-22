"""Deterministic, read-only access to structured records.

This is the only sanctioned way to read accounts/orders/tickets out of the
SQLite database. Every function here:

- takes an explicit sqlite3.Connection (no hidden global connection/state)
- uses parameterized SQL exclusively — there is no function anywhere in this
  module that accepts a SQL string or fragment from a caller, so an LLM tool
  built on top of this module has no way to run arbitrary SQL
- returns a typed Pydantic model (see app/backend/models/records.py) or
  None, never a bare sqlite3.Row
- returns None (single record) or [] (list) for "not found" — a record that
  exists but has null fields is still returned, just with those fields None

Account scoping. Full authentication/authorization is not implemented yet
(see docs/architecture.md, Phase 5). To avoid an interface change later,
every function accepts an optional `allowed_account_ids` collection. When
given, a record belonging to an account outside that collection is treated
exactly like a record that does not exist — callers cannot distinguish
"forbidden" from "not found", which avoids leaking account existence to a
scope that shouldn't see it. When omitted (the default), no scoping is
applied, which is correct for Phase 2's own tests and for any future
internal/admin caller that is allowed to see everything.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Iterable
from typing import Any

from app.backend.models.records import (
    Account,
    DatasetMetadata,
    Order,
    SourceProvenance,
    Ticket,
)


def _in_scope(account_id: str, allowed_account_ids: Collection[str] | None) -> bool:
    return allowed_account_ids is None or account_id in allowed_account_ids


def _row_to_account(row: sqlite3.Row) -> Account:
    return Account(
        account_id=row["account_id"],
        account_name=row["account_name"],
        plan=row["plan"],
        status=row["status"],
        csm=row["csm"],
        contract_file=row["contract_file"],
        premium_support=None if row["premium_support"] is None else bool(row["premium_support"]),
        notes=row["notes"],
    )


def _row_to_order(row: sqlite3.Row) -> Order:
    return Order(
        order_id=row["order_id"],
        account_id=row["account_id"],
        carrier=row["carrier"],
        status=row["status"],
        booked_at=row["booked_at"],
        pickup_window_start=row["pickup_window_start"],
        pickup_window_end=row["pickup_window_end"],
        pickup_actual_at=row["pickup_actual_at"],
        shipment_fee_inr=row["shipment_fee_inr"],
        carrier_fault=None if row["carrier_fault"] is None else bool(row["carrier_fault"]),
        customer_fault=None if row["customer_fault"] is None else bool(row["customer_fault"]),
        cancellation_requested_at=row["cancellation_requested_at"],
        notes=row["notes"],
    )


def _row_to_ticket(row: sqlite3.Row) -> Ticket:
    return Ticket(
        ticket_id=row["ticket_id"],
        account_id=row["account_id"],
        created_at=row["created_at"],
        status=row["status"],
        subject=row["subject"],
        description=row["description"],
        channel=row["channel"],
        assigned_to=row["assigned_to"],
        last_customer_message_at=row["last_customer_message_at"],
        historical_resolution=row["historical_resolution"],
    )


def get_all_account_ids(conn: sqlite3.Connection) -> list[str]:
    """Every account id present in the ingested dataset, sorted.

    Used by the Phase 5 mock auth directory to give internal staff an explicit
    account scope rather than an unrestricted one, without hard-coding a
    single account id in application code. Deliberately ids only: this is an
    authorization input, not a data read.
    """
    rows = conn.execute("SELECT account_id FROM accounts ORDER BY account_id").fetchall()
    return [row["account_id"] for row in rows]


def get_account(
    conn: sqlite3.Connection,
    account_id: str,
    *,
    allowed_account_ids: Collection[str] | None = None,
) -> Account | None:
    row = conn.execute(
        "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
    ).fetchone()
    if row is None:
        return None
    if not _in_scope(row["account_id"], allowed_account_ids):
        return None
    return _row_to_account(row)


def get_order(
    conn: sqlite3.Connection,
    order_id: str,
    *,
    allowed_account_ids: Collection[str] | None = None,
) -> Order | None:
    row = conn.execute(
        "SELECT * FROM orders WHERE order_id = ?", (order_id,)
    ).fetchone()
    if row is None:
        return None
    if not _in_scope(row["account_id"], allowed_account_ids):
        return None
    return _row_to_order(row)


def get_ticket(
    conn: sqlite3.Connection,
    ticket_id: str,
    *,
    allowed_account_ids: Collection[str] | None = None,
) -> Ticket | None:
    row = conn.execute(
        "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
    ).fetchone()
    if row is None:
        return None
    if not _in_scope(row["account_id"], allowed_account_ids):
        return None
    return _row_to_ticket(row)


def get_account_orders(
    conn: sqlite3.Connection,
    account_id: str,
    *,
    allowed_account_ids: Collection[str] | None = None,
) -> list[Order]:
    if not _in_scope(account_id, allowed_account_ids):
        return []
    rows = conn.execute(
        "SELECT * FROM orders WHERE account_id = ? ORDER BY order_id", (account_id,)
    ).fetchall()
    return [_row_to_order(r) for r in rows]


def get_account_tickets(
    conn: sqlite3.Connection,
    account_id: str,
    *,
    allowed_account_ids: Collection[str] | None = None,
) -> list[Ticket]:
    if not _in_scope(account_id, allowed_account_ids):
        return []
    rows = conn.execute(
        "SELECT * FROM tickets WHERE account_id = ? ORDER BY ticket_id", (account_id,)
    ).fetchall()
    return [_row_to_ticket(r) for r in rows]


def get_dataset_metadata(conn: sqlite3.Connection) -> DatasetMetadata | None:
    row = conn.execute("SELECT * FROM dataset_metadata WHERE id = 1").fetchone()
    if row is None:
        return None
    return DatasetMetadata(
        dataset_snapshot_raw=row["dataset_snapshot_raw"],
        dataset_snapshot_at=row["dataset_snapshot_at"],
        dataset_timezone=row["dataset_timezone"],
        currency=row["currency"],
        notes=row["notes"],
        important_note=row["important_note"],
        source_workbook_filename=row["source_workbook_filename"],
        source_workbook_sha256=row["source_workbook_sha256"],
        source_sheet_names=json.loads(row["source_sheet_names"]),
        ingested_at_utc=row["ingested_at_utc"],
        ingestion_script_version=row["ingestion_script_version"],
    )


def get_source_provenance(
    conn: sqlite3.Connection, target_table: str, target_id: str
) -> SourceProvenance | None:
    """Explain where a structured value came from: file, sheet, row, and the
    raw cell values as originally read (pre-parsing) for that row."""
    row = conn.execute(
        "SELECT * FROM source_provenance WHERE target_table = ? AND target_id = ?",
        (target_table, target_id),
    ).fetchone()
    if row is None:
        return None
    return SourceProvenance(
        target_table=row["target_table"],
        target_id=row["target_id"],
        source_file=row["source_file"],
        source_sheet=row["source_sheet"],
        source_row_number=row["source_row_number"],
        raw_row=json.loads(row["raw_row_json"]),
    )
