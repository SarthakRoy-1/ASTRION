"""SQLite schema creation and connection management.

This module owns the *shape* of the structured-data layer only: table
definitions, pragmas, and connection setup. It has no knowledge of the
source workbook or how rows get loaded — that is scripts/ingest_dataset.py's
job. Keeping schema separate from ingestion means the schema can be created
(e.g. in tests) without ever touching data/source/.

Design notes (see docs/architecture.md for the full rationale):

- Tables are declared STRICT (SQLite >= 3.37) so a coding mistake that binds
  the wrong Python type raises immediately instead of silently coercing.
- Foreign keys are enforced (PRAGMA foreign_keys = ON per connection; SQLite
  does not persist this pragma in the database file).
- Timestamps are stored as TEXT in ISO 8601 with an explicit UTC offset
  (e.g. "2026-08-16T09:00:00+05:30"), never as naive values. The offset is
  driven entirely by the timezone named in the workbook's own README sheet
  (see scripts/ingest_dataset.py) — nothing here hard-codes a zone.
- This layer stores facts only. No SLA/cancellation/service-credit
  calculation lives here or anywhere under app/backend/services/ — see
  docs/architecture.md section 2 for the boundary.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = REPO_ROOT / "data" / "processed" / "parcelpilot.db"

# Executed in order. Children (FK-bearing tables) after the parents they
# reference; junction/audit tables last.
SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS dataset_metadata (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        dataset_snapshot_raw TEXT NOT NULL,
        dataset_snapshot_at TEXT NOT NULL,
        dataset_timezone TEXT NOT NULL,
        currency TEXT,
        notes TEXT,
        important_note TEXT,
        source_workbook_filename TEXT NOT NULL,
        source_workbook_sha256 TEXT NOT NULL,
        source_sheet_names TEXT NOT NULL,
        ingested_at_utc TEXT NOT NULL,
        ingestion_script_version TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS ingestion_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at_utc TEXT NOT NULL,
        finished_at_utc TEXT,
        source_workbook_path TEXT NOT NULL,
        source_workbook_sha256 TEXT NOT NULL,
        ingestion_script_version TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running'
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS accounts (
        account_id TEXT PRIMARY KEY,
        account_name TEXT,
        plan TEXT,
        status TEXT,
        csm TEXT,
        contract_file TEXT,
        premium_support INTEGER,
        notes TEXT
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts (account_id),
        carrier TEXT,
        status TEXT,
        booked_at TEXT,
        pickup_window_start TEXT,
        pickup_window_end TEXT,
        pickup_actual_at TEXT,
        shipment_fee_inr REAL,
        carrier_fault INTEGER,
        customer_fault INTEGER,
        cancellation_requested_at TEXT,
        notes TEXT
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS tickets (
        ticket_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts (account_id),
        created_at TEXT,
        status TEXT,
        subject TEXT,
        description TEXT,
        channel TEXT,
        assigned_to TEXT,
        last_customer_message_at TEXT,
        historical_resolution TEXT
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS source_provenance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ingestion_run_id INTEGER NOT NULL REFERENCES ingestion_runs (id),
        target_table TEXT NOT NULL,
        target_id TEXT NOT NULL,
        source_file TEXT NOT NULL,
        source_sheet TEXT NOT NULL,
        source_row_number INTEGER NOT NULL,
        raw_row_json TEXT NOT NULL,
        UNIQUE (target_table, target_id)
    ) STRICT
    """,
    # --- Phase 3: document / evidence layer ---------------------------------
    #
    # Deliberately independent of the Phase 2 tables above. In particular
    # `documents.account_id` is NOT a foreign key to accounts, and document
    # provenance does NOT reuse `source_provenance`: scripts/ingest_dataset.py
    # deletes from accounts and source_provenance on every run, so either
    # coupling would make the two ingestion scripts order-dependent and let a
    # dataset reload silently destroy document provenance. The account link is
    # instead cross-checked at ingestion time (see scripts/ingest_documents.py),
    # matching how Phase 2 already treats accounts.contract_file as a soft
    # reference to a filename rather than a row.
    """
    CREATE TABLE IF NOT EXISTS document_ingestion_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at_utc TEXT NOT NULL,
        finished_at_utc TEXT,
        source_dir TEXT NOT NULL,
        document_count INTEGER,
        chunk_count INTEGER,
        ingestion_script_version TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running'
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS documents (
        document_id TEXT PRIMARY KEY,
        source_file TEXT NOT NULL UNIQUE,
        source_sha256 TEXT NOT NULL,
        title TEXT NOT NULL,
        document_type TEXT NOT NULL,
        status TEXT NOT NULL,
        status_raw TEXT NOT NULL,
        is_current INTEGER NOT NULL,
        is_deprecated INTEGER NOT NULL,
        is_authoritative INTEGER NOT NULL,
        authority_tier INTEGER NOT NULL,
        account_id TEXT,
        customer_name TEXT,
        plan TEXT,
        effective_date_raw TEXT,
        effective_date TEXT,
        updated_date_raw TEXT,
        updated_date TEXT,
        term_raw TEXT,
        term_start TEXT,
        term_end TEXT,
        supersedes TEXT,
        superseded_by TEXT,
        page_count INTEGER NOT NULL,
        ingestion_run_id INTEGER NOT NULL
            REFERENCES document_ingestion_runs (id)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS document_chunks (
        chunk_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL REFERENCES documents (document_id),
        chunk_ordinal INTEGER NOT NULL,
        page_number INTEGER NOT NULL,
        section_number TEXT,
        section_title TEXT,
        subsection_title TEXT,
        section_path TEXT,
        topic TEXT NOT NULL,
        text TEXT NOT NULL,
        char_count INTEGER NOT NULL,
        word_count INTEGER NOT NULL,
        page_char_start INTEGER NOT NULL,
        page_char_end INTEGER NOT NULL,
        UNIQUE (document_id, chunk_ordinal)
    ) STRICT
    """,
    # --- Phase 4: agent actions ---------------------------------------------
    #
    # `agent_actions` is the confirmation state machine, persisted. An action
    # is prepared into PENDING_CONFIRMATION and only a separate, explicit
    # confirmation moves it to EXECUTED — see app/backend/services/actions.py.
    #
    # Effects land in dedicated tables (`ticket_escalations`, `ticket_notes`)
    # rather than mutating `tickets`. The Phase 2 tables are regenerated from
    # the workbook on every ingest run, so an executed action written into
    # them would be silently reverted by the next reload. For the same reason
    # `target_id` is a soft reference, not a foreign key — exactly as
    # documents.account_id is (see the Phase 3 note above).
    """
    CREATE TABLE IF NOT EXISTS agent_actions (
        action_id TEXT PRIMARY KEY,
        action_type TEXT NOT NULL,
        status TEXT NOT NULL,
        account_id TEXT,
        target_type TEXT NOT NULL,
        target_id TEXT NOT NULL,
        parameters_json TEXT NOT NULL,
        preview TEXT NOT NULL,
        reason TEXT,
        evidence_chunk_ids_json TEXT NOT NULL,
        requested_by TEXT NOT NULL,
        requested_by_role TEXT NOT NULL,
        -- The conversation the proposal was produced in (Phase 5). Nullable:
        -- a Phase 4 caller with no session still prepares actions normally.
        -- Bound at preparation and re-checked at confirmation, so a proposal
        -- cannot be confirmed from a different conversation.
        session_id TEXT,
        confirmed_by TEXT,
        prepared_at_utc TEXT NOT NULL,
        expires_at_utc TEXT NOT NULL,
        confirmed_at_utc TEXT,
        executed_at_utc TEXT,
        rejected_at_utc TEXT,
        result_json TEXT,
        failure_reason TEXT
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS ticket_escalations (
        escalation_id TEXT PRIMARY KEY,
        action_id TEXT NOT NULL REFERENCES agent_actions (action_id),
        ticket_id TEXT NOT NULL,
        account_id TEXT,
        severity TEXT,
        reason TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at_utc TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS ticket_notes (
        note_id TEXT PRIMARY KEY,
        action_id TEXT NOT NULL REFERENCES agent_actions (action_id),
        ticket_id TEXT NOT NULL,
        account_id TEXT,
        note TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at_utc TEXT NOT NULL
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_orders_account_id ON orders (account_id)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_account_id ON tickets (account_id)",
    "CREATE INDEX IF NOT EXISTS idx_documents_account_id ON documents (account_id)",
    "CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON document_chunks (document_id)",
    "CREATE INDEX IF NOT EXISTS idx_chunks_topic ON document_chunks (topic)",
    "CREATE INDEX IF NOT EXISTS idx_actions_status ON agent_actions (status)",
    "CREATE INDEX IF NOT EXISTS idx_actions_target ON agent_actions (target_type, target_id)",
)


def get_connection(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a connection with row access by column name and FKs enforced."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after a table's first release. `CREATE TABLE IF NOT EXISTS`
# silently does nothing on an existing table, so a database built by an earlier
# phase would otherwise keep the old shape forever. Kept as an explicit,
# additive list: every entry must be nullable or defaulted, because it is
# applied to tables that already hold rows.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("agent_actions", "session_id", "TEXT"),
)


def _apply_added_columns(conn: sqlite3.Connection) -> None:
    for table, column, declaration in ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:  # table absent entirely; CREATE TABLE covers it
            continue
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def initialize_schema(conn: sqlite3.Connection) -> None:
    """Create all tables/indexes if they do not already exist. Idempotent.

    Also brings an already-created database up to the current column set — see
    `ADDED_COLUMNS`. Both halves are safe to run repeatedly and safe to run on
    a database built by an earlier phase.

    The identity/tenancy/audit tables are created here too, from
    `app/backend/auth/schema.py`. They are declared in that module rather than
    this one because they have the opposite lifecycle to everything above: the
    dataset tables are rebuilt from the workbook on every ingest run, and the
    security tables hold the only copy of their data and must survive one.
    """
    from app.backend.auth.schema import SECURITY_SCHEMA_STATEMENTS

    with conn:
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
        for statement in SECURITY_SCHEMA_STATEMENTS:
            conn.execute(statement)
        _apply_added_columns(conn)
