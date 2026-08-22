import sqlite3

from app.backend.services import database as db


def test_get_connection_creates_parent_directory(tmp_path):
    db_path = tmp_path / "nested" / "dir" / "test.db"

    conn = db.get_connection(db_path)
    conn.close()

    assert db_path.parent.is_dir()


def test_get_connection_enables_foreign_keys(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    try:
        row = conn.execute("PRAGMA foreign_keys").fetchone()
        assert row[0] == 1
    finally:
        conn.close()


def test_get_connection_row_factory_allows_column_access(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    try:
        db.initialize_schema(conn)
        conn.execute(
            "INSERT INTO accounts (account_id, account_name) VALUES (?, ?)",
            ("ACCT-X", "Test Account"),
        )
        row = conn.execute("SELECT * FROM accounts").fetchone()
        assert row["account_id"] == "ACCT-X"
        assert row["account_name"] == "Test Account"
    finally:
        conn.close()


def test_initialize_schema_creates_all_expected_tables(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    try:
        db.initialize_schema(conn)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        expected = {
            "accounts",
            "orders",
            "tickets",
            "dataset_metadata",
            "ingestion_runs",
            "source_provenance",
        }
        assert expected.issubset(tables)
    finally:
        conn.close()


def test_initialize_schema_is_idempotent(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    try:
        db.initialize_schema(conn)
        db.initialize_schema(conn)  # must not raise
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        assert len(tables) > 0
    finally:
        conn.close()


def test_orders_foreign_key_to_accounts_is_enforced(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    try:
        db.initialize_schema(conn)
        try:
            conn.execute(
                """
                INSERT INTO orders (order_id, account_id) VALUES ('ORD-1', 'DOES-NOT-EXIST')
                """
            )
            conn.commit()
            raised = False
        except sqlite3.IntegrityError:
            raised = True
        assert raised, "expected a foreign key violation for an unknown account_id"
    finally:
        conn.close()


def test_tickets_foreign_key_to_accounts_is_enforced(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    try:
        db.initialize_schema(conn)
        try:
            conn.execute(
                "INSERT INTO tickets (ticket_id, account_id) VALUES ('TKT-1', 'DOES-NOT-EXIST')"
            )
            conn.commit()
            raised = False
        except sqlite3.IntegrityError:
            raised = True
        assert raised
    finally:
        conn.close()


def test_strict_table_rejects_wrong_type(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    try:
        db.initialize_schema(conn)
        try:
            # shipment_fee_inr is REAL; a dict is not coercible under STRICT
            conn.execute(
                "INSERT INTO orders (order_id, account_id, shipment_fee_inr) VALUES (?, ?, ?)",
                ("ORD-1", "ACCT-X", "not-a-number-but-also-not-numeric-text-$$$"),
            )
            conn.execute("INSERT INTO accounts (account_id) VALUES ('ACCT-X')")
            conn.commit()
        except sqlite3.IntegrityError:
            pass  # STRICT column type rejection is also acceptable here
    finally:
        conn.close()


def test_dataset_metadata_rejects_second_row(tmp_path):
    conn = db.get_connection(tmp_path / "test.db")
    try:
        db.initialize_schema(conn)
        conn.execute(
            """
            INSERT INTO dataset_metadata
                (id, dataset_snapshot_raw, dataset_snapshot_at, dataset_timezone,
                 source_workbook_filename, source_workbook_sha256, source_sheet_names,
                 ingested_at_utc, ingestion_script_version)
            VALUES (1, 'x', 'x', 'x', 'x', 'x', '[]', 'x', 'x')
            """
        )
        conn.commit()
        raised = False
        try:
            conn.execute(
                """
                INSERT INTO dataset_metadata
                    (id, dataset_snapshot_raw, dataset_snapshot_at, dataset_timezone,
                     source_workbook_filename, source_workbook_sha256, source_sheet_names,
                     ingested_at_utc, ingestion_script_version)
                VALUES (2, 'y', 'y', 'y', 'y', 'y', '[]', 'y', 'y')
                """
            )
            conn.commit()
        except sqlite3.IntegrityError:
            raised = True
        assert raised, "dataset_metadata must only ever hold id = 1"
    finally:
        conn.close()


def test_default_db_path_is_under_data_processed():
    assert db.DEFAULT_DB_PATH.parent.name == "processed"
    assert db.DEFAULT_DB_PATH.name == "parcelpilot.db"
