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
