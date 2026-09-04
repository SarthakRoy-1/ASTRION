"""Phase 3: loading the six supplied PDFs into SQLite.

Covers the ingestion script end to end, its cross-check against Phase 2's
accounts data, and — importantly — that the two ingestion scripts do not
interfere with each other in either order.
"""

import shutil

import pytest

from app.backend.services.database import get_connection, initialize_schema
from app.backend.services.documents import get_latest_document_ingestion_run, list_documents
from app.backend.retrieval.extraction import DocumentIngestionError
from conftest import (
    CURRENT_POLICY_DOC_ID,
    DEPRECATED_POLICY_DOC_ID,
    LUMENWORKS_PDF,
    NORTHSTAR_DOC_ID,
    NORTHSTAR_PDF,
    SOURCE_DIR,
    default_sheets,
    write_workbook,
)
from scripts import ingest_dataset, ingest_documents

EXPECTED_DOCUMENT_COUNT = 6


def _fresh_db(tmp_path, name="docs.db"):
    return tmp_path / name


# --- successful ingestion -------------------------------------------------------


def test_ingests_all_six_pdfs(tmp_path):
    result = ingest_documents.ingest(source_dir=SOURCE_DIR, db_path=_fresh_db(tmp_path))

    assert result["ok"] is True
    assert result["counts"]["documents"] == EXPECTED_DOCUMENT_COUNT
    assert result["counts"]["chunks"] > 0


def test_every_expected_document_is_present(doc_conn):
    documents = list_documents(doc_conn)

    assert len(documents) == EXPECTED_DOCUMENT_COUNT
    assert {d.source_file for d in documents} == {
        "01_Support_Policy_v3_CURRENT.pdf",
        "02_Support_Policy_v2_DEPRECATED.pdf",
        "03_Cancellation_and_Service_Credit_SOP_v4.pdf",
        "04_Product_Operations_Guide_and_Known_Issues.pdf",
        "05_Northstar_Logistics_Enterprise_Agreement.pdf",
        "06_LumenWorks_Service_Agreement.pdf",
    }


def test_every_document_has_at_least_one_chunk(doc_conn):
    rows = doc_conn.execute(
        """
        SELECT d.document_id, COUNT(c.chunk_id) AS n
        FROM documents d LEFT JOIN document_chunks c USING (document_id)
        GROUP BY d.document_id
        """
    ).fetchall()

    assert len(rows) == EXPECTED_DOCUMENT_COUNT
    assert all(r["n"] > 0 for r in rows)


def test_document_metadata_is_persisted(doc_conn):
    row = doc_conn.execute(
        "SELECT * FROM documents WHERE document_id = ?", (NORTHSTAR_DOC_ID,)
    ).fetchone()

    assert row["account_id"] == "ACCT-001"
    assert row["customer_name"] == "Northstar Logistics"
    assert row["status"] == "ACTIVE"
    assert row["authority_tier"] == 1
    assert row["is_authoritative"] == 1
    assert row["term_start"] == "2026-01-01"
    assert len(row["source_sha256"]) == 64


def test_deprecated_document_is_persisted_as_non_authoritative(doc_conn):
    row = doc_conn.execute(
        "SELECT * FROM documents WHERE document_id = ?", (DEPRECATED_POLICY_DOC_ID,)
    ).fetchone()

    assert row["is_deprecated"] == 1
    assert row["is_authoritative"] == 0
    assert row["authority_tier"] == 4


def test_general_documents_have_null_account(doc_conn):
    rows = doc_conn.execute(
        "SELECT account_id FROM documents WHERE document_type != 'customer_agreement'"
    ).fetchall()

    assert all(r["account_id"] is None for r in rows)


def test_chunk_provenance_columns_are_populated(doc_conn):
    rows = doc_conn.execute("SELECT * FROM document_chunks").fetchall()

    assert rows
    for row in rows:
        assert row["page_number"] >= 1
        assert row["text"].strip()
        assert row["topic"]
        assert row["page_char_end"] > row["page_char_start"]


def test_ingestion_run_is_recorded(doc_conn):
    run = get_latest_document_ingestion_run(doc_conn)

    assert run is not None
    assert run["status"] == "success"
    assert run["document_count"] == EXPECTED_DOCUMENT_COUNT
    assert run["finished_at_utc"] is not None


# --- idempotency -------------------------------------------------------------------


def test_repeat_ingestion_keeps_counts_stable(tmp_path):
    db_path = _fresh_db(tmp_path)

    first = ingest_documents.ingest(source_dir=SOURCE_DIR, db_path=db_path)
    second = ingest_documents.ingest(source_dir=SOURCE_DIR, db_path=db_path)
    third = ingest_documents.ingest(source_dir=SOURCE_DIR, db_path=db_path)

    assert first["counts"] == second["counts"] == third["counts"]

    conn = get_connection(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == EXPECTED_DOCUMENT_COUNT
        # The audit log is append-only by design: one row per run.
        assert conn.execute("SELECT COUNT(*) FROM document_ingestion_runs").fetchone()[0] == 3
    finally:
        conn.close()


def test_chunk_ids_are_stable_across_reingestion(tmp_path):
    db_path = _fresh_db(tmp_path)

    ingest_documents.ingest(source_dir=SOURCE_DIR, db_path=db_path)
    conn = get_connection(db_path)
    before = conn.execute(
        "SELECT chunk_id, text, page_number, section_path FROM document_chunks ORDER BY chunk_id"
    ).fetchall()
    conn.close()

    ingest_documents.ingest(source_dir=SOURCE_DIR, db_path=db_path)
    conn = get_connection(db_path)
    after = conn.execute(
        "SELECT chunk_id, text, page_number, section_path FROM document_chunks ORDER BY chunk_id"
    ).fetchall()
    conn.close()

    assert [tuple(r) for r in before] == [tuple(r) for r in after]


def test_ingestion_does_not_modify_source_pdfs(tmp_path):
    import hashlib

    before = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in SOURCE_DIR.glob("*.pdf")
    }

    ingest_documents.ingest(source_dir=SOURCE_DIR, db_path=_fresh_db(tmp_path))

    after = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in SOURCE_DIR.glob("*.pdf")
    }
    assert before == after


# --- missing / malformed source ------------------------------------------------------


def test_missing_pdf_fails_clearly(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for pdf in SOURCE_DIR.glob("*.pdf"):
        shutil.copy(pdf, source / pdf.name)
    (source / NORTHSTAR_PDF).unlink()

    with pytest.raises(DocumentIngestionError, match="not found"):
        ingest_documents.ingest(source_dir=source, db_path=_fresh_db(tmp_path))


def test_corrupt_pdf_fails_clearly(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for pdf in SOURCE_DIR.glob("*.pdf"):
        shutil.copy(pdf, source / pdf.name)
    (source / LUMENWORKS_PDF).write_bytes(b"not a pdf")

    # Rejected on its magic bytes before pymupdf is invoked, so the message is
    # the type mismatch rather than a parser failure. Either way the ingestion
    # refuses rather than storing a partially-read document.
    with pytest.raises(DocumentIngestionError, match="contents are|could not be opened"):
        ingest_documents.ingest(source_dir=source, db_path=_fresh_db(tmp_path))


def test_failed_ingestion_leaves_previous_load_intact(tmp_path):
    db_path = _fresh_db(tmp_path)
    ingest_documents.ingest(source_dir=SOURCE_DIR, db_path=db_path)

    broken = tmp_path / "broken_source"
    broken.mkdir()
    for pdf in SOURCE_DIR.glob("*.pdf"):
        shutil.copy(pdf, broken / pdf.name)
    (broken / LUMENWORKS_PDF).write_bytes(b"not a pdf")

    with pytest.raises(DocumentIngestionError):
        ingest_documents.ingest(source_dir=broken, db_path=db_path)

    conn = get_connection(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == EXPECTED_DOCUMENT_COUNT
    finally:
        conn.close()


def test_main_returns_nonzero_on_failure(tmp_path):
    exit_code = ingest_documents.main(
        ["--source-dir", str(tmp_path / "nope"), "--db", str(_fresh_db(tmp_path)), "--json"]
    )

    assert exit_code == 1


def test_main_returns_zero_on_success(tmp_path):
    exit_code = ingest_documents.main(
        ["--source-dir", str(SOURCE_DIR), "--db", str(_fresh_db(tmp_path)), "--json"]
    )

    assert exit_code == 0


# --- cross-check against Phase 2 accounts data ------------------------------------------


def test_cross_check_is_skipped_when_accounts_are_absent(tmp_path):
    """Document ingestion must work standalone, before the workbook is loaded."""
    result = ingest_documents.ingest(source_dir=SOURCE_DIR, db_path=_fresh_db(tmp_path))

    assert any("skipped" in note for note in result["account_link_notes"])


def test_cross_check_confirms_matching_accounts(tmp_path):
    db_path = _fresh_db(tmp_path)
    conn = get_connection(db_path)
    initialize_schema(conn)
    conn.execute(
        "INSERT INTO accounts (account_id, contract_file) VALUES (?, ?)",
        ("ACCT-001", NORTHSTAR_PDF),
    )
    conn.execute(
        "INSERT INTO accounts (account_id, contract_file) VALUES (?, ?)",
        ("ACCT-002", LUMENWORKS_PDF),
    )
    conn.commit()
    conn.close()

    result = ingest_documents.ingest(source_dir=SOURCE_DIR, db_path=db_path)

    assert any("ACCT-001 confirmed" in note for note in result["account_link_notes"])
    assert any("ACCT-002 confirmed" in note for note in result["account_link_notes"])


def test_cross_check_notes_but_allows_account_absent_from_workbook(tmp_path):
    """The two layers load from independent sources and may be different
    vintages; a workbook that simply lacks a customer must not block document
    ingestion."""
    db_path = _fresh_db(tmp_path)
    conn = get_connection(db_path)
    initialize_schema(conn)
    conn.execute("INSERT INTO accounts (account_id, contract_file) VALUES ('ACCT-XYZ', NULL)")
    conn.commit()
    conn.close()

    result = ingest_documents.ingest(source_dir=SOURCE_DIR, db_path=db_path)

    assert result["counts"]["documents"] == EXPECTED_DOCUMENT_COUNT
    assert any("not cross-checked" in note for note in result["account_link_notes"])


def test_cross_check_rejects_contract_file_disagreement(tmp_path):
    db_path = _fresh_db(tmp_path)
    conn = get_connection(db_path)
    initialize_schema(conn)
    conn.execute(
        "INSERT INTO accounts (account_id, contract_file) VALUES ('ACCT-001', 'some_other.pdf')"
    )
    conn.execute("INSERT INTO accounts (account_id, contract_file) VALUES ('ACCT-002', NULL)")
    conn.commit()
    conn.close()

    with pytest.raises(DocumentIngestionError, match="source data disagrees"):
        ingest_documents.ingest(source_dir=SOURCE_DIR, db_path=db_path)


# --- coexistence with the Phase 2 pipeline ------------------------------------------------


def test_dataset_reload_does_not_destroy_documents(tmp_path):
    """scripts/ingest_dataset.py deletes from accounts and source_provenance on
    every run. Document rows and document provenance must survive that."""
    db_path = _fresh_db(tmp_path)
    workbook = tmp_path / "data.xlsx"
    write_workbook(workbook, default_sheets())

    ingest_dataset.ingest(workbook_path=workbook, db_path=db_path)
    ingest_documents.ingest(source_dir=SOURCE_DIR, db_path=db_path)
    ingest_dataset.ingest(workbook_path=workbook, db_path=db_path)  # reload the workbook

    conn = get_connection(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == EXPECTED_DOCUMENT_COUNT
        assert conn.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 2
    finally:
        conn.close()


def test_document_reload_does_not_destroy_structured_data(tmp_path):
    db_path = _fresh_db(tmp_path)
    workbook = tmp_path / "data.xlsx"
    write_workbook(workbook, default_sheets())

    ingest_documents.ingest(source_dir=SOURCE_DIR, db_path=db_path)
    ingest_dataset.ingest(workbook_path=workbook, db_path=db_path)
    ingest_documents.ingest(source_dir=SOURCE_DIR, db_path=db_path)  # reload documents

    conn = get_connection(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM source_provenance").fetchone()[0] == 6
    finally:
        conn.close()


def test_either_ingestion_order_reaches_the_same_state(tmp_path):
    workbook = tmp_path / "data.xlsx"
    write_workbook(workbook, default_sheets())

    documents_first = tmp_path / "a.db"
    ingest_documents.ingest(source_dir=SOURCE_DIR, db_path=documents_first)
    ingest_dataset.ingest(workbook_path=workbook, db_path=documents_first)

    dataset_first = tmp_path / "b.db"
    ingest_dataset.ingest(workbook_path=workbook, db_path=dataset_first)
    ingest_documents.ingest(source_dir=SOURCE_DIR, db_path=dataset_first)

    def snapshot(path):
        conn = get_connection(path)
        try:
            return {
                "documents": conn.execute(
                    "SELECT document_id, source_sha256, authority_tier FROM documents "
                    "ORDER BY document_id"
                ).fetchall(),
                "chunks": conn.execute(
                    "SELECT chunk_id, text FROM document_chunks ORDER BY chunk_id"
                ).fetchall(),
                "accounts": conn.execute(
                    "SELECT account_id FROM accounts ORDER BY account_id"
                ).fetchall(),
            }
        finally:
            conn.close()

    a, b = snapshot(documents_first), snapshot(dataset_first)
    assert [tuple(r) for r in a["documents"]] == [tuple(r) for r in b["documents"]]
    assert [tuple(r) for r in a["chunks"]] == [tuple(r) for r in b["chunks"]]
    assert [tuple(r) for r in a["accounts"]] == [tuple(r) for r in b["accounts"]]
