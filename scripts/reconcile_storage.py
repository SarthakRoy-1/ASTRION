#!/usr/bin/env python
"""Reconcile object storage with the documents table.

Finds objects nothing references, documents whose object is missing, and retries
removals the application recorded as failed. By default it only reports; pass
`--remove-unreferenced` to delete objects that are unreferenced and older than
the grace period (an object younger than that may be an upload still in flight).

    python scripts/reconcile_storage.py [--remove-unreferenced] [--grace-minutes 60]

Uses DATABASE_URL and the STORAGE_* settings, like the API. Prints keys and
counts only, never credentials. Exits 1 if anything needs attention.
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.backend.core.config import load_settings  # noqa: E402
from app.backend.db import open_database  # noqa: E402
from app.backend.services.storage_reconcile import reconcile  # noqa: E402
from app.backend.storage import open_store  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remove-unreferenced", action="store_true")
    parser.add_argument("--grace-minutes", type=int, default=60)
    args = parser.parse_args(argv)

    settings = load_settings()
    database = open_database(settings)
    store = open_store(settings)
    conn = database.connect()
    try:
        report = reconcile(
            conn,
            store,
            remove_unreferenced=args.remove_unreferenced,
            grace=timedelta(minutes=args.grace_minutes),
        )
    finally:
        conn.close()
        database.close()

    print(f"documents checked:            {report.documents_checked}")
    print(f"recorded orphans resolved:    {report.orphans_resolved}")
    print(f"recorded orphans still stuck: {len(report.orphans_failed)}")
    print(f"unreferenced objects:         {len(report.unreferenced)} (removed {report.unreferenced_removed})")
    print(f"documents missing an object:  {len(report.missing_objects)}")
    for key in report.unreferenced:
        print(f"  unreferenced: {key}")
    for document_id in report.missing_objects:
        print(f"  missing object for document: {document_id}")
    return 0 if report.clean else 1


if __name__ == "__main__":
    sys.exit(main())
