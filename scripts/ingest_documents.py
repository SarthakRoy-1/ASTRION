"""Load the six supplied PDFs into the document/evidence layer.

Reads data/source/*.pdf, extracts metadata and section-aware chunks, and
writes them into data/processed/astrion.db. Only reads data/source/ —
never writes to it.

Like scripts/ingest_dataset.py, this validates everything in memory first and
only then opens a transaction, so a malformed document fails loudly and
leaves any existing database untouched. Re-running is safe: each run replaces
`documents` and `document_chunks` wholesale, so chunk ids and row counts are
stable across runs. `document_ingestion_runs` is an append-only audit log and
grows by one row per run by design.

This script produces evidence, not answers. It records which source outranks
which (authority tiers), but computes no SLA deadline, cancellation fee, or
service credit — those are Phase 4. See docs/architecture.md.

Usage:
    python scripts/ingest_documents.py [--source-dir PATH] [--db PATH] [--json]

Exit codes:
    0  ingestion succeeded
    1  a source document failed validation, or ingestion otherwise failed
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.backend.models.documents import Document, DocumentChunk  # noqa: E402
from app.backend.retrieval.extraction import (  # noqa: E402
    DocumentIngestionError,
    ExtractedDocument,
    extract_document,
)
from app.backend.services.database import (  # noqa: E402
    DEFAULT_DB_PATH,
    get_connection,
    initialize_schema,
)
from scripts.inspect_sources import PDF_FILES  # noqa: E402
from scripts.verify_source_pack import sha256_of  # noqa: E402

DEFAULT_SOURCE_DIR = REPO_ROOT / "data" / "source"

INGESTION_SCRIPT_VERSION = "1.0.0"


def extract_all(source_dir: Path) -> list[ExtractedDocument]:
    """Extract every expected PDF, in declared order. Raises on the first
    missing, unreadable, or metadata-less document."""
    extracted: list[ExtractedDocument] = []
    for name in PDF_FILES:
        path = source_dir / name
        # Check existence before hashing: sha256_of would otherwise raise a
        # bare FileNotFoundError and bypass this module's error type.
        if not path.is_file():
            raise DocumentIngestionError(f"source document not found: {path}")
        extracted.append(extract_document(path, source_sha256=sha256_of(path)))
    return extracted


def validate_account_links(conn: sqlite3.Connection, documents: list[Document]) -> list[str]:
    """Cross-check each agreement's `Account:` against Phase 2's accounts data.

    Skipped when the accounts table is absent or empty — document ingestion
    must work standalone, before or without the workbook having been loaded.

    Two outcomes are deliberately graded differently:

    - An agreement naming an account the workbook does not contain is only
      *noted*. The two layers are loaded from independent sources and may be
      different vintages; refusing to ingest documents because the workbook
      is a subset would re-create exactly the ordering coupling that keeping
      account_id out of the foreign-key graph was meant to avoid. Account
      isolation is unaffected — it keys on the document's own stated account.
    - Both sources describing the *same* account but disagreeing about which
      file is its contract is a genuine contradiction, and fails loudly.
    """
    try:
        rows = conn.execute("SELECT account_id, contract_file FROM accounts").fetchall()
    except sqlite3.OperationalError:
        return ["accounts table not present — account cross-check skipped"]
    if not rows:
        return ["accounts table empty — account cross-check skipped"]

    contract_by_account = {r["account_id"]: r["contract_file"] for r in rows}
    notes: list[str] = []
    for document in documents:
        if document.account_id is None:
            continue
        if document.account_id not in contract_by_account:
            notes.append(
                f"{document.source_file}: states 'Account: {document.account_id}', which is not "
                f"present in the accounts table — not cross-checked"
            )
            continue
        expected_file = contract_by_account[document.account_id]
        if expected_file is not None and expected_file != document.source_file:
            raise DocumentIngestionError(
                f"{document.source_file}: states 'Account: {document.account_id}', but that "
                f"account's contract_file is {expected_file!r} — source data disagrees"
            )
        notes.append(f"{document.source_file} ↔ {document.account_id} confirmed against accounts")
    return notes


def _iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _insert_document(conn: sqlite3.Connection, document: Document, run_id: int) -> None:
    conn.execute(
        """
        INSERT INTO documents
            (document_id, source_file, source_sha256, title, document_type, status,
             status_raw, is_current, is_deprecated, is_authoritative, authority_tier,
             account_id, customer_name, plan, effective_date_raw, effective_date,
             updated_date_raw, updated_date, term_raw, term_start, term_end,
             supersedes, superseded_by, page_count, ingestion_run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document.document_id, document.source_file, document.source_sha256,
            document.title, str(document.document_type.value), str(document.status.value),
            document.status_raw, int(document.is_current), int(document.is_deprecated),
            int(document.is_authoritative), int(document.authority_tier),
            document.account_id, document.customer_name, document.plan,
            document.effective_date_raw, _iso(document.effective_date),
            document.updated_date_raw, _iso(document.updated_date),
            document.term_raw, _iso(document.term_start), _iso(document.term_end),
            document.supersedes, document.superseded_by, int(document.page_count), int(run_id),
        ),
    )


def _insert_chunk(conn: sqlite3.Connection, chunk: DocumentChunk) -> None:
    conn.execute(
        """
        INSERT INTO document_chunks
            (chunk_id, document_id, chunk_ordinal, page_number, section_number,
             section_title, subsection_title, section_path, topic, text,
             char_count, word_count, page_char_start, page_char_end)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chunk.chunk_id, chunk.document_id, int(chunk.chunk_ordinal),
            int(chunk.page_number), chunk.section_number, chunk.section_title,
            chunk.subsection_title, chunk.section_path, str(chunk.topic.value),
            chunk.text, int(chunk.char_count), int(chunk.word_count),
            int(chunk.page_char_start), int(chunk.page_char_end),
        ),
    )


def load_into_db(
    conn: sqlite3.Connection, extracted: list[ExtractedDocument], *, source_dir: Path
) -> dict[str, int]:
    """Replace the document tables inside a single transaction."""
    started = datetime.now(timezone.utc).isoformat()

    with conn:
        cursor = conn.execute(
            """
            INSERT INTO document_ingestion_runs
                (started_at_utc, source_dir, ingestion_script_version, status)
            VALUES (?, ?, ?, 'running')
            """,
            (started, str(source_dir), INGESTION_SCRIPT_VERSION),
        )
        run_id = cursor.lastrowid

        conn.execute("DELETE FROM document_chunks")
        conn.execute("DELETE FROM documents")

        chunk_total = 0
        for item in extracted:
            _insert_document(conn, item.document, run_id)
            for chunk in item.chunks:
                _insert_chunk(conn, chunk)
            chunk_total += len(item.chunks)

        conn.execute(
            """
            UPDATE document_ingestion_runs
               SET finished_at_utc = ?, status = 'success',
                   document_count = ?, chunk_count = ?
             WHERE id = ?
            """,
            (datetime.now(timezone.utc).isoformat(), len(extracted), chunk_total, run_id),
        )

    return {"documents": len(extracted), "chunks": chunk_total}


def ingest(
    source_dir: Path = DEFAULT_SOURCE_DIR, db_path: Path = DEFAULT_DB_PATH
) -> dict[str, Any]:
    source_dir = Path(source_dir)
    db_path = Path(db_path)

    extracted = extract_all(source_dir)

    conn = get_connection(db_path)
    try:
        initialize_schema(conn)
        notes = validate_account_links(conn, [e.document for e in extracted])
        counts = load_into_db(conn, extracted, source_dir=source_dir)
    finally:
        conn.close()

    return {
        "ok": True,
        "source_dir": str(source_dir),
        "database": str(db_path),
        "counts": counts,
        "account_link_notes": notes,
        "documents": [
            {
                "document_id": e.document.document_id,
                "source_file": e.document.source_file,
                "title": e.document.title,
                "document_type": e.document.document_type.value,
                "status": e.document.status.value,
                "authority_tier": int(e.document.authority_tier),
                "is_authoritative": e.document.is_authoritative,
                "account_id": e.document.account_id,
                "pages": e.document.page_count,
                "chunks": len(e.chunks),
            }
            for e in extracted
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = ingest(source_dir=args.source_dir, db_path=args.db)
    except DocumentIngestionError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            print(f"DOCUMENT INGESTION FAILED: {exc}")
        return 1

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"Source: {result['source_dir']}")
        print(f"Database: {result['database']}")
        for doc in result["documents"]:
            scope = doc["account_id"] or "general"
            print(
                f"  [tier {doc['authority_tier']}] {doc['source_file']:50s} "
                f"{doc['status']:10s} {scope:9s} pages={doc['pages']} chunks={doc['chunks']}"
            )
        print(
            f"  totals: {result['counts']['documents']} document(s), "
            f"{result['counts']['chunks']} chunk(s)"
        )
        for note in result["account_link_notes"]:
            print(f"  note: {note}")
        print("DOCUMENT INGESTION OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
