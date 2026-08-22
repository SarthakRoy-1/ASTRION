"""Pydantic models for structured records returned by the services layer.

These are read-side representations of rows in the SQLite database created by
app/backend/services/database.py and populated by scripts/ingest_dataset.py.
They are not an ORM: nothing here writes to the database, and nothing here
maps Python objects back to SQL. They exist so that
app/backend/services/records.py has a typed, validated shape to return
instead of raw sqlite3.Row objects.

Nullable fields here mirror nullability actually observed in the source
workbook (see data/processed/source_inspection.json) — a field is Optional
because the source data can be blank for it, not by default caution.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class Account(BaseModel):
    model_config = ConfigDict(frozen=True)

    account_id: str
    account_name: str | None
    plan: str | None
    status: str | None
    csm: str | None
    contract_file: str | None
    premium_support: bool | None
    notes: str | None


class Order(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str
    account_id: str
    carrier: str | None
    status: str | None
    booked_at: datetime | None
    pickup_window_start: datetime | None
    pickup_window_end: datetime | None
    pickup_actual_at: datetime | None
    shipment_fee_inr: float | None
    carrier_fault: bool | None
    customer_fault: bool | None
    cancellation_requested_at: datetime | None
    notes: str | None


class Ticket(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticket_id: str
    account_id: str
    created_at: datetime | None
    status: str | None
    subject: str | None
    description: str | None
    channel: str | None
    assigned_to: str | None
    last_customer_message_at: datetime | None
    historical_resolution: str | None


class DatasetMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    dataset_snapshot_raw: str
    dataset_snapshot_at: datetime
    dataset_timezone: str
    currency: str | None
    notes: str | None
    important_note: str | None
    source_workbook_filename: str
    source_workbook_sha256: str
    source_sheet_names: list[str]
    ingested_at_utc: datetime
    ingestion_script_version: str


class SourceProvenance(BaseModel):
    model_config = ConfigDict(frozen=True)

    target_table: str
    target_id: str
    source_file: str
    source_sheet: str
    source_row_number: int
    raw_row: dict[str, object]
