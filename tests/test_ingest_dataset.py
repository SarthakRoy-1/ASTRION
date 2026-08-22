from datetime import timezone, timedelta

import pytest

from app.backend.services.database import get_connection
from conftest import SAMPLE_CONTRACT_FILE, default_sheets, write_workbook
from scripts import ingest_dataset


def _ingest(tmp_path, sheets=None, workbook_name="data.xlsx", db_name="test.db"):
    workbook_path = tmp_path / workbook_name
    write_workbook(workbook_path, sheets if sheets is not None else default_sheets())
    db_path = tmp_path / db_name
    result = ingest_dataset.ingest(workbook_path=workbook_path, db_path=db_path)
    return result, db_path, workbook_path


# --- happy path ----------------------------------------------------------


def test_successful_ingestion_reports_expected_row_counts(tmp_path):
    result, db_path, _ = _ingest(tmp_path)

    assert result["ok"] is True
    assert result["row_counts"] == {"accounts": 2, "orders": 2, "tickets": 2}


def test_ingestion_creates_a_readable_database(tmp_path):
    _, db_path, _ = _ingest(tmp_path)

    conn = get_connection(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 2
    finally:
        conn.close()


def test_repeat_ingestion_does_not_change_row_counts(tmp_path):
    sheets = default_sheets()
    workbook_path = tmp_path / "data.xlsx"
    write_workbook(workbook_path, sheets)
    db_path = tmp_path / "test.db"

    result_1 = ingest_dataset.ingest(workbook_path=workbook_path, db_path=db_path)
    result_2 = ingest_dataset.ingest(workbook_path=workbook_path, db_path=db_path)
    result_3 = ingest_dataset.ingest(workbook_path=workbook_path, db_path=db_path)

    assert result_1["row_counts"] == result_2["row_counts"] == result_3["row_counts"]

    conn = get_connection(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM source_provenance").fetchone()[0] == 6  # 2+2+2
        # ingestion_runs is an append-only audit log: one row added per run
        assert conn.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM dataset_metadata").fetchone()[0] == 1
    finally:
        conn.close()


def test_ingestion_never_modifies_the_workbook(tmp_path):
    from scripts.verify_source_pack import sha256_of

    _, _, workbook_path = _ingest(tmp_path)
    before = sha256_of(workbook_path)

    ingest_dataset.ingest(workbook_path=workbook_path, db_path=tmp_path / "second.db")

    after = sha256_of(workbook_path)
    assert before == after


# --- timestamps ------------------------------------------------------------


def test_timestamps_are_parsed_with_dataset_timezone(tmp_path):
    _, db_path, _ = _ingest(tmp_path)

    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT booked_at FROM orders WHERE order_id = 'ORD-1'").fetchone()
        assert row["booked_at"] == "2026-01-10T09:00:00+05:30"
    finally:
        conn.close()


def test_dataset_snapshot_timezone_is_read_from_readme_not_hardcoded(tmp_path):
    sheets = default_sheets()
    sheets["README"][0] = ["Dataset snapshot", "2026-01-10 09:00 America/New_York"]
    result, db_path, _ = _ingest(tmp_path, sheets=sheets)

    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT booked_at FROM orders WHERE order_id = 'ORD-1'").fetchone()
        assert row["booked_at"].endswith("-05:00") or row["booked_at"].endswith("-04:00")
        meta = conn.execute("SELECT dataset_timezone FROM dataset_metadata").fetchone()
        assert meta["dataset_timezone"] == "America/New_York"
    finally:
        conn.close()


def test_malformed_timestamp_fails_ingestion(tmp_path):
    sheets = default_sheets()
    sheets["orders"][1][4] = "16/01/2026 09:00"  # wrong format for booked_at

    with pytest.raises(ingest_dataset.IngestionError, match="malformed timestamp"):
        _ingest(tmp_path, sheets=sheets)


def test_impossible_calendar_date_fails_ingestion(tmp_path):
    sheets = default_sheets()
    sheets["orders"][1][4] = "2026-02-30 09:00"  # Feb 30 does not exist

    with pytest.raises(ingest_dataset.IngestionError):
        _ingest(tmp_path, sheets=sheets)


def test_unknown_timezone_in_snapshot_fails_ingestion(tmp_path):
    sheets = default_sheets()
    sheets["README"][0] = ["Dataset snapshot", "2026-01-10 09:00 Nowhere/Fake"]

    with pytest.raises(ingest_dataset.IngestionError, match="unknown timezone"):
        _ingest(tmp_path, sheets=sheets)


def test_failed_ingestion_leaves_no_database_behind_on_first_run(tmp_path):
    sheets = default_sheets()
    sheets["orders"][1][4] = "not-a-timestamp"
    workbook_path = tmp_path / "data.xlsx"
    write_workbook(workbook_path, sheets)
    db_path = tmp_path / "test.db"

    with pytest.raises(ingest_dataset.IngestionError):
        ingest_dataset.ingest(workbook_path=workbook_path, db_path=db_path)

    assert not db_path.exists()


def test_failed_re_ingestion_does_not_corrupt_existing_database(tmp_path):
    result_1, db_path, workbook_path = _ingest(tmp_path)

    bad_sheets = default_sheets()
    bad_sheets["orders"][1][4] = "not-a-timestamp"
    write_workbook(workbook_path, bad_sheets)

    with pytest.raises(ingest_dataset.IngestionError):
        ingest_dataset.ingest(workbook_path=workbook_path, db_path=db_path)

    conn = get_connection(db_path)
    try:
        # the last *successful* load is still intact
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 2
    finally:
        conn.close()


# --- structural validation --------------------------------------------------


def test_missing_required_sheet_fails_ingestion(tmp_path):
    sheets = default_sheets()
    del sheets["tickets"]

    with pytest.raises(ingest_dataset.IngestionError, match="missing required sheet"):
        _ingest(tmp_path, sheets=sheets)


def test_unexpected_sheet_fails_ingestion(tmp_path):
    sheets = default_sheets()
    sheets["extra_sheet"] = [["a"], ["b"]]

    with pytest.raises(ingest_dataset.IngestionError, match="unexpected sheet"):
        _ingest(tmp_path, sheets=sheets)


def test_missing_required_column_fails_ingestion(tmp_path):
    sheets = default_sheets()
    header = sheets["accounts"][0]
    col_index = header.index("csm")
    for row in sheets["accounts"]:
        del row[col_index]

    with pytest.raises(ingest_dataset.IngestionError, match="missing required column"):
        _ingest(tmp_path, sheets=sheets)


def test_unexpected_column_fails_ingestion(tmp_path):
    sheets = default_sheets()
    sheets["accounts"][0] = sheets["accounts"][0] + ["extra_column"]
    sheets["accounts"][1] = sheets["accounts"][1] + ["surprise"]
    sheets["accounts"][2] = sheets["accounts"][2] + ["surprise"]

    with pytest.raises(ingest_dataset.IngestionError, match="unexpected column"):
        _ingest(tmp_path, sheets=sheets)


def test_orphaned_order_account_reference_fails_ingestion(tmp_path):
    sheets = default_sheets()
    sheets["orders"][1][1] = "ACCT-DOES-NOT-EXIST"

    with pytest.raises(ingest_dataset.IngestionError, match="does not match any row"):
        _ingest(tmp_path, sheets=sheets)


def test_orphaned_ticket_account_reference_fails_ingestion(tmp_path):
    sheets = default_sheets()
    sheets["tickets"][1][1] = "ACCT-DOES-NOT-EXIST"

    with pytest.raises(ingest_dataset.IngestionError, match="does not match any row"):
        _ingest(tmp_path, sheets=sheets)


def test_duplicate_account_id_fails_ingestion(tmp_path):
    sheets = default_sheets()
    sheets["accounts"].append(list(sheets["accounts"][1]))  # duplicate ACCT-A

    with pytest.raises(ingest_dataset.IngestionError, match="duplicate account_id"):
        _ingest(tmp_path, sheets=sheets)


def test_unknown_contract_file_fails_ingestion(tmp_path):
    sheets = default_sheets()
    sheets["accounts"][1][5] = "not_a_real_supplied_pdf.pdf"

    with pytest.raises(ingest_dataset.IngestionError, match="not one of the supplied source PDFs"):
        _ingest(tmp_path, sheets=sheets)


def test_missing_workbook_fails_ingestion(tmp_path):
    with pytest.raises(ingest_dataset.IngestionError, match="workbook not found"):
        ingest_dataset.ingest(workbook_path=tmp_path / "does_not_exist.xlsx", db_path=tmp_path / "t.db")


# --- null handling -----------------------------------------------------------


def test_null_contract_file_is_preserved_as_null(tmp_path):
    _, db_path, _ = _ingest(tmp_path)

    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT contract_file FROM accounts WHERE account_id = 'ACCT-B'").fetchone()
        assert row["contract_file"] is None
    finally:
        conn.close()


def test_null_pickup_actual_at_is_preserved_as_null(tmp_path):
    _, db_path, _ = _ingest(tmp_path)

    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT pickup_actual_at FROM orders WHERE order_id = 'ORD-1'").fetchone()
        assert row["pickup_actual_at"] is None
    finally:
        conn.close()


def test_null_historical_resolution_is_preserved_as_null(tmp_path):
    _, db_path, _ = _ingest(tmp_path)

    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT historical_resolution FROM tickets WHERE ticket_id = 'TKT-1'").fetchone()
        assert row["historical_resolution"] is None
        other = conn.execute("SELECT historical_resolution FROM tickets WHERE ticket_id = 'TKT-2'").fetchone()
        assert other["historical_resolution"] == "Told customer X."
    finally:
        conn.close()


# --- metadata & provenance ----------------------------------------------------


def test_dataset_metadata_is_captured(tmp_path):
    _, db_path, _ = _ingest(tmp_path)

    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT * FROM dataset_metadata WHERE id = 1").fetchone()
        assert row["dataset_snapshot_raw"] == "2026-01-10 09:00 Asia/Kolkata"
        assert row["dataset_snapshot_at"] == "2026-01-10T09:00:00+05:30"
        assert row["dataset_timezone"] == "Asia/Kolkata"
        assert row["currency"] == "INR"
        assert row["source_workbook_filename"] == "data.xlsx"
        assert row["ingestion_script_version"] == ingest_dataset.INGESTION_SCRIPT_VERSION
        import json
        assert json.loads(row["source_sheet_names"]) == ["README", "accounts", "orders", "tickets"]
    finally:
        conn.close()


def test_provenance_preserves_original_raw_value_not_parsed_value(tmp_path):
    _, db_path, _ = _ingest(tmp_path)

    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM source_provenance WHERE target_table = 'orders' AND target_id = 'ORD-1'"
        ).fetchone()
        assert row["source_file"] == "data.xlsx"
        assert row["source_sheet"] == "orders"
        assert row["source_row_number"] == 2  # header is row 1

        import json
        raw = json.loads(row["raw_row_json"])
        # the raw snapshot must hold the ORIGINAL "YYYY-MM-DD HH:MM" text,
        # not the ISO-with-offset value stored in orders.booked_at
        assert raw["booked_at"] == "2026-01-10 09:00"
    finally:
        conn.close()


def test_every_row_has_provenance(tmp_path):
    _, db_path, _ = _ingest(tmp_path)

    conn = get_connection(db_path)
    try:
        counts = dict(
            conn.execute(
                "SELECT target_table, COUNT(*) FROM source_provenance GROUP BY target_table"
            ).fetchall()
        )
        assert counts == {"accounts": 2, "orders": 2, "tickets": 2}
    finally:
        conn.close()


# --- injection safety through the ingestion path ------------------------------


def test_sql_injection_style_cell_value_is_stored_verbatim(tmp_path):
    sheets = default_sheets()
    payload = "Robert'); DROP TABLE accounts; --"
    sheets["accounts"][1][7] = payload  # notes column

    _, db_path, _ = _ingest(tmp_path, sheets=sheets)

    conn = get_connection(db_path)
    try:
        # table still exists and the payload is stored as inert text
        row = conn.execute("SELECT notes FROM accounts WHERE account_id = 'ACCT-A'").fetchone()
        assert row["notes"] == payload
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 2
    finally:
        conn.close()
