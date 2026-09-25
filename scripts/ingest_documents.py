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
from app.backend.auth.repository import ensure_organization  # noqa: E402
from app.backend.db import open_connection  # noqa: E402
from app.backend.services.database import initialize_schema  # noqa: E402
from app.backend.tenancy import LEGACY_ORG_ID  # noqa: E402
from app.backend.services.document_ingestion import (  # noqa: E402
    load_into_db,
    validate_account_links,
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




def store_originals(conn, store, extracted, *, source_dir: Path, org_id: str | None) -> int:
    """Put each source PDF in the document store and record where.

    The object goes first, then the row learns its key, so a row never names an
    object that is not there. Idempotent: an unchanged file has the same key and
    is simply written again. General documents land under `system/`, agreements
    under their workspace.
    """
    from app.backend.services.document_ingestion import owner_of
    from app.backend.storage import document_key

    stored = 0
    for item in extracted:
        document = item.document
        data = (source_dir / document.source_file).read_bytes()
        key = document_key(owner_of(document, org_id), document.document_id, document.source_sha256)
        store.put(key, data, content_type="application/pdf")
        with conn:
            conn.execute(
                "UPDATE documents SET storage_key = ?, original_filename = ?, "
                "content_type = 'application/pdf', size_bytes = ? WHERE document_id = ?",
                (key, document.source_file, len(data), document.document_id),
            )
        stored += 1
    return stored


def ingest(
    source_dir: Path = DEFAULT_SOURCE_DIR,
    db_path: Path | None = None,
    org_id: str | None = LEGACY_ORG_ID,
    store=None,
) -> dict[str, Any]:
    """Load the source pack: general documents as system documents, agreements
    for `org_id`.

    Pass `org_id=None` to load system documents only (a production deployment,
    where no workspace owns the assessment agreements); the customer-specific
    agreements are then skipped rather than attached to anyone.
    """
    source_dir = Path(source_dir)

    extracted = extract_all(source_dir)
    if org_id is None:
        extracted = [e for e in extracted if e.document.account_id is None]

    conn = open_connection(db_path)
    try:
        initialize_schema(conn)
        if org_id == LEGACY_ORG_ID:
            ensure_organization(
                conn, org_id=org_id, name="Assessment dataset (legacy)", slug="assessment-legacy"
            )
        notes = validate_account_links(conn, [e.document for e in extracted], org_id)
        counts = load_into_db(conn, extracted, source_dir=source_dir, org_id=org_id)
        if store is not None:
            counts["objects_stored"] = store_originals(
                conn, store, extracted, source_dir=source_dir, org_id=org_id
            )
    finally:
        conn.close()

    return {
        "ok": True,
        "source_dir": str(source_dir),
        "database": str(db_path) if db_path is not None else "DATABASE_URL",
        "org_id": org_id,
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
    parser.add_argument(
        "--db", type=Path, default=None,
        help="A SQLite file. Omit to use DATABASE_URL (PostgreSQL in production).",
    )
    parser.add_argument(
        "--org-id", default=LEGACY_ORG_ID,
        help="The workspace that owns the customer agreements in the pack.",
    )
    parser.add_argument(
        "--system-only", action="store_true",
        help="Load only the general documents, as system documents visible to "
             "every workspace. Skips the customer-specific agreements.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        store = None
        if args.db is None:
            # Running against the configured database (production): keep the
            # originals in the configured object store as well.
            from app.backend.core.config import load_settings
            from app.backend.storage import open_store

            store = open_store(load_settings())
        result = ingest(
            source_dir=args.source_dir,
            db_path=args.db,
            org_id=None if args.system_only else args.org_id,
            store=store,
        )
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
