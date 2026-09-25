"""Reusable document ingestion logic.

Extracted from scripts/ingest_documents.py to support single-file uploads via
the API while preserving the ability to reload the entire corpus or specific directories.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from app.backend.db.errors import OperationalError
from app.backend.models.documents import Document, DocumentChunk
from app.backend.retrieval.extraction import (
    DocumentIngestionError,
    ExtractedDocument,
)

INGESTION_SCRIPT_VERSION = "1.0.0"


def validate_account_links(
    conn: sqlite3.Connection, documents: list[Document], org_id: str | None
) -> list[str]:
    """Cross-check each agreement's `Account:` against the workspace's accounts."""
    if org_id is None:
        return ["no workspace named — account cross-check skipped"]
    try:
        rows = conn.execute(
            "SELECT account_id, contract_file FROM accounts WHERE org_id = ?", (org_id,)
        ).fetchall()
    except OperationalError:
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
        notes.append(f"{document.source_file} \u2194 {document.account_id} confirmed against accounts")
    return notes


def _iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _insert_document(
    conn: sqlite3.Connection, document: Document, run_id: int, org_id: str | None
) -> None:
    """Store one document under its owner. `org_id=None` is a system document."""
    conn.execute(
        """
        INSERT INTO documents
            (document_id, org_id, source_file, source_sha256, title, document_type, status,
             status_raw, is_current, is_deprecated, is_authoritative, authority_tier,
             account_id, customer_name, plan, effective_date_raw, effective_date,
             updated_date_raw, updated_date, term_raw, term_start, term_end,
             supersedes, superseded_by, page_count, ingestion_run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document.document_id, org_id, document.source_file, document.source_sha256,
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


def ingest_single_document(
    conn: sqlite3.Connection,
    extracted: ExtractedDocument,
    source_dir: str,
    *,
    org_id: str,
) -> dict[str, int]:
    """Ingest one document for a workspace, in a single transaction.

    Replaces the document if the workspace already has it. A document id that
    already exists under a *different* owner is never replaced: an upload cannot
    overwrite another workspace's (or the platform's) document by colliding on
    its id.
    """
    if not org_id:
        raise DocumentIngestionError("a document must be owned by a workspace")
    started = datetime.now(timezone.utc).isoformat()

    with conn:
        existing = conn.execute(
            "SELECT org_id FROM documents WHERE document_id = ?",
            (extracted.document.document_id,),
        ).fetchone()
        if existing is not None and existing["org_id"] != org_id:
            raise DocumentIngestionError(
                f"{extracted.document.source_file}: that document id belongs to another owner"
            )

        cursor = conn.execute(
            """
            INSERT INTO document_ingestion_runs
                (org_id, started_at_utc, source_dir, ingestion_script_version, status)
            VALUES (?, ?, ?, ?, 'running')
            RETURNING id
            """,
            (org_id, started, source_dir, INGESTION_SCRIPT_VERSION),
        )
        run_id = cursor.fetchall()[0]["id"]

        conn.execute("DELETE FROM document_chunks WHERE document_id = ?", (extracted.document.document_id,))
        conn.execute("DELETE FROM documents WHERE document_id = ?", (extracted.document.document_id,))

        _insert_document(conn, extracted.document, run_id, org_id)
        for chunk in extracted.chunks:
            _insert_chunk(conn, chunk)

        conn.execute(
            """
            UPDATE document_ingestion_runs
               SET finished_at_utc = ?, status = 'success',
                   document_count = ?, chunk_count = ?
             WHERE id = ?
            """,
            (datetime.now(timezone.utc).isoformat(), 1, len(extracted.chunks), run_id),
        )

    return {"documents": 1, "chunks": len(extracted.chunks)}


def owner_of(document: Document, org_id: str | None) -> str | None:
    """Who owns a source-pack document: the platform, or the workspace it is for.

    A general document (it names no account) is platform knowledge and becomes a
    system document, visible to every workspace. A customer agreement names an
    account, and an account only exists inside a workspace, so it belongs to
    `org_id` -- and cannot be loaded without one.
    """
    if document.account_id is None:
        return None
    if org_id is None:
        raise DocumentIngestionError(
            f"{document.source_file}: a customer-specific document needs a workspace "
            "to belong to (pass one, or load system documents only)"
        )
    return org_id


def load_into_db(
    conn: sqlite3.Connection,
    extracted: list[ExtractedDocument],
    *,
    source_dir: Path,
    org_id: str | None,
) -> dict[str, int]:
    """Replace the source pack's documents inside a single transaction.

    General documents are stored as system documents (no owner); agreements for
    a named account are stored as `org_id`'s. Only rows from this source
    directory that are system documents or that workspace's own are replaced.
    """
    started = datetime.now(timezone.utc).isoformat()
    owners = {item.document.document_id: owner_of(item.document, org_id) for item in extracted}

    with conn:
        cursor = conn.execute(
            """
            INSERT INTO document_ingestion_runs
                (org_id, started_at_utc, source_dir, ingestion_script_version, status)
            VALUES (?, ?, ?, ?, 'running')
            RETURNING id
            """,
            (org_id, started, str(source_dir), INGESTION_SCRIPT_VERSION),
        )
        run_id = cursor.fetchall()[0]["id"]

        # Delete ONLY the documents that originated from the same source_dir
        # This preserves uploaded documents when scripts/ingest_documents.py runs,
        # and preserves canonical documents when reindexing uploads.
        conn.execute(
            """
            DELETE FROM document_chunks
             WHERE document_id IN (
                 SELECT d.document_id FROM documents d
                 JOIN document_ingestion_runs r ON d.ingestion_run_id = r.id
                 WHERE r.source_dir = ? AND (d.org_id IS NULL OR d.org_id = ?)
             )
            """,
            (str(source_dir), org_id)
        )
        conn.execute(
            """
            DELETE FROM documents
             WHERE document_id IN (
                 SELECT d.document_id FROM documents d
                 JOIN document_ingestion_runs r ON d.ingestion_run_id = r.id
                 WHERE r.source_dir = ? AND (d.org_id IS NULL OR d.org_id = ?)
             )
            """,
            (str(source_dir), org_id)
        )

        # And replace, by identity, whatever is about to be inserted. Matching on
        # `source_dir` alone is a string comparison, so the same corpus loaded
        # once as an absolute path and once as a relative one (or from a
        # container's /app and a laptop's checkout) would find nothing to delete
        # and then collide on the documents' own unique keys. Identity is what
        # "replace" has to mean.
        incoming_ids = [item.document.document_id for item in extracted]
        incoming_files = [item.document.source_file for item in extracted]
        if incoming_ids:
            id_marks = ",".join("?" * len(incoming_ids))
            file_marks = ",".join("?" * len(incoming_files))
            match = f"document_id IN ({id_marks}) OR source_file IN ({file_marks})"
            conn.execute(
                f"DELETE FROM document_chunks WHERE document_id IN "
                f"(SELECT document_id FROM documents WHERE {match})",
                [*incoming_ids, *incoming_files],
            )
            conn.execute(
                f"DELETE FROM documents WHERE {match}",
                [*incoming_ids, *incoming_files],
            )

        chunk_total = 0
        for item in extracted:
            _insert_document(conn, item.document, run_id, owners[item.document.document_id])
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
