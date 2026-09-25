"""Reconcile the document store with the documents table.

The two are updated in separate steps (see `document_files`), so a crash or a
network failure between them can leave an object nobody references, or a row
whose object has gone. This finds both, retries what the application already
knew it had failed to clean up, and reports what it cannot fix.

Run it on a schedule (`python scripts/reconcile_storage.py`) or by hand. It never
deletes an object it has not first proved is unreferenced, and it leaves anything
younger than a grace period alone: an object that was just written and whose row
has not committed yet is an upload in flight, not an orphan.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.backend.storage import DocumentStore

DEFAULT_GRACE = timedelta(hours=1)
_SCANNED_PREFIXES = ("workspaces/", "system/")


@dataclass
class ReconcileReport:
    #: Recorded orphans whose objects are now gone (removed, or already absent).
    orphans_resolved: int = 0
    #: Recorded orphans that still could not be removed.
    orphans_failed: list[str] = field(default_factory=list)
    #: Objects in the store that no document references and are past the grace period.
    unreferenced: list[str] = field(default_factory=list)
    unreferenced_removed: int = 0
    #: Documents whose recorded object is missing. Reported, never guessed at.
    missing_objects: list[str] = field(default_factory=list)
    documents_checked: int = 0

    @property
    def clean(self) -> bool:
        return not (self.orphans_failed or self.unreferenced or self.missing_objects)


def reconcile(
    conn: sqlite3.Connection,
    store: DocumentStore,
    *,
    remove_unreferenced: bool = False,
    grace: timedelta = DEFAULT_GRACE,
    now: datetime | None = None,
) -> ReconcileReport:
    now = now or datetime.now(timezone.utc)
    report = ReconcileReport()
    referenced = {
        row["storage_key"]
        for row in conn.execute("SELECT storage_key FROM documents WHERE storage_key IS NOT NULL")
    }

    # 1. Objects the application already knew it had failed to remove.
    pending = conn.execute(
        "SELECT storage_key FROM storage_orphans WHERE resolved_at_utc IS NULL"
    ).fetchall()
    for row in pending:
        key = row["storage_key"]
        try:
            if key not in referenced:
                store.delete(key)
        except Exception:  # noqa: BLE001
            report.orphans_failed.append(key)
            continue
        with conn:
            conn.execute(
                "UPDATE storage_orphans SET resolved_at_utc = ? WHERE storage_key = ?",
                (now.isoformat(), key),
            )
        report.orphans_resolved += 1

    # 2. Objects nothing references: an upload that never got a row.
    for prefix in _SCANNED_PREFIXES:
        for info in store.list(prefix):
            if info.key in referenced:
                continue
            if info.last_modified is not None and now - info.last_modified < grace:
                continue  # possibly an upload in flight
            report.unreferenced.append(info.key)
            if remove_unreferenced:
                try:
                    store.delete(info.key)
                    report.unreferenced_removed += 1
                except Exception:  # noqa: BLE001
                    report.orphans_failed.append(info.key)

    # 3. Rows whose object has gone. There is nothing to repair them from, so say so.
    for row in conn.execute(
        "SELECT document_id, storage_key FROM documents WHERE storage_key IS NOT NULL"
    ).fetchall():
        report.documents_checked += 1
        if not store.exists(row["storage_key"]):
            report.missing_objects.append(row["document_id"])

    return report
