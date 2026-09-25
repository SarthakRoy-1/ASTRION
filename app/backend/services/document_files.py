"""A document's file and its record, kept consistent.

PostgreSQL holds the metadata and extracted text; the original file is an object
in the document store. Those are two systems with no transaction across them, so
the order of operations is what keeps them honest. The rules, each of which the
tests hold this module to:

1. **A row never points at a missing object.** The object is written *before*
   the transaction that creates the row.
2. **A failed creation leaves no object behind.** If the transaction fails, the
   object just written is removed -- unless another row already uses that key
   (a re-upload of identical bytes), in which case removing it would destroy a
   document that is fine.
3. **Nothing is removed before its replacement is durable.** A delete removes the
   row first and the object after the commit; a replacement removes the old
   object only once the new row is committed.
4. **A failed cleanup is recorded, not lost.** If an object cannot be removed
   when it should be, its key goes into `storage_orphans` for `reconcile` to
   retry, and the request that hit it still succeeds -- a network blip on
   cleanup must not turn a completed upload into an error.
5. **Bytes are checked against their checksum** whenever they are read back.

A file's key contains its sha256, so the same bytes always land at the same key
and different bytes never overwrite them.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.backend.retrieval.extraction import (
    DocumentIngestionError,
    ExtractedDocument,
    extract_document,
)
from app.backend.services.document_ingestion import StorageInfo, ingest_single_document
from app.backend.storage import ChecksumMismatch, DocumentStore, ObjectNotFound, document_key

logger = logging.getLogger("astrion.documents")


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def extract_from_bytes(
    content: bytes, *, source_file: str, expected_sha256: str | None = None
) -> ExtractedDocument:
    """Extract a document that is in memory, via a private temporary file.

    The parser wants a path; the file exists only for the duration of the call,
    in a directory nobody else can see, and is gone whether extraction succeeds
    or raises. Nothing about the local disk outlives the request.
    """
    digest = sha256_bytes(content)
    if expected_sha256 is not None and digest != expected_sha256:
        raise ChecksumMismatch("the file does not match its recorded checksum")
    # `ignore_cleanup_errors`: on Windows a parser that failed to open a file can
    # still hold it, and a leftover temp file must not turn a clean rejection
    # ("this is not a PDF") into a server error.
    with tempfile.TemporaryDirectory(
        prefix="astrion-extract-", ignore_cleanup_errors=True
    ) as directory:
        path = Path(directory) / source_file
        path.write_bytes(content)
        return extract_document(path, source_sha256=digest)


def _referenced(conn: sqlite3.Connection, key: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM documents WHERE storage_key = ? LIMIT 1", (key,)
    ).fetchone() is not None


def record_orphan(
    conn: sqlite3.Connection, key: str, *, org_id: str | None, reason: str
) -> None:
    """Remember an object that should be removed and could not be. Never raises."""
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO storage_orphans
                    (orphan_id, storage_key, org_id, reason, recorded_at_utc)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (storage_key) DO UPDATE
                    SET reason = excluded.reason, resolved_at_utc = NULL
                """,
                (
                    f"ORPH-{uuid.uuid4().hex[:12]}",
                    key,
                    org_id,
                    reason,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    except Exception:  # noqa: BLE001 - the record of a failure must not raise a second one
        logger.exception("could not record orphaned object %s", key)


def remove_object(
    conn: sqlite3.Connection,
    store: DocumentStore,
    key: str | None,
    *,
    org_id: str | None,
    reason: str,
) -> bool:
    """Remove an object nothing needs; record it if that fails. Returns whether it is gone."""
    if not key:
        return True
    if _referenced(conn, key):
        return True  # another document still uses these exact bytes
    try:
        store.delete(key)
        return True
    except Exception:  # noqa: BLE001 - see rule 4
        logger.warning("could not remove object %s (%s); recorded for reconciliation", key, reason)
        record_orphan(conn, key, org_id=org_id, reason=reason)
        return False


def store_and_ingest(
    conn: sqlite3.Connection,
    store: DocumentStore,
    *,
    org_id: str,
    content: bytes,
    original_filename: str,
    content_type: str,
    extracted: ExtractedDocument,
    source_dir: str,
) -> dict:
    """Put the file in the store, then create (or replace) the document's row.

    Returns `{"documents": 1, "chunks": n}`; the caller never sees a storage key.
    """
    digest = sha256_bytes(content)
    if digest != extracted.document.source_sha256:
        raise DocumentIngestionError("the extracted document does not match the file")
    key = document_key(org_id, extracted.document.document_id, digest)

    stored = store.put(key, content, content_type=content_type)  # rule 1: before the row
    info = StorageInfo(
        storage_key=key,
        original_filename=original_filename,
        content_type=content_type,
        size_bytes=stored.size,
    )
    try:
        result = ingest_single_document(
            conn, extracted, source_dir, org_id=org_id, storage=info
        )
    except BaseException:
        remove_object(conn, store, key, org_id=org_id, reason="ingest_failed")  # rule 2
        raise

    old = result.get("replaced_storage_key")
    if old and old != key:
        # Rule 3: the replaced object goes only now that the new row is committed.
        remove_object(conn, store, old, org_id=org_id, reason="replaced")
    return {"documents": result["documents"], "chunks": result["chunks"]}


def delete_document_and_object(
    conn: sqlite3.Connection,
    store: DocumentStore,
    *,
    delete_row,
    storage_key: str | None,
    org_id: str | None,
) -> bool:
    """Delete a document's row, then its object (rule 3). `delete_row` does the first."""
    deleted = delete_row()
    if deleted:
        remove_object(conn, store, storage_key, org_id=org_id, reason="deleted")
    return deleted


def read_verified(store: DocumentStore, key: str, expected_sha256: str) -> bytes:
    """Read an object and confirm it is the bytes that were recorded (rule 5)."""
    data = store.get(key)
    if sha256_bytes(data) != expected_sha256:
        raise ChecksumMismatch(f"object {key} does not match its recorded checksum")
    return data


__all__ = [
    "ChecksumMismatch",
    "ObjectNotFound",
    "delete_document_and_object",
    "extract_from_bytes",
    "read_verified",
    "record_orphan",
    "remove_object",
    "sha256_bytes",
    "store_and_ingest",
]
