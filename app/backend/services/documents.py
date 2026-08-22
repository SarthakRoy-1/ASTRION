"""Deterministic, read-only access to documents and document chunks.

The document-layer counterpart to app/backend/services/records.py, and it
follows the same rules: explicit connection, parameterized SQL only, typed
models out, no function anywhere accepts a SQL string from a caller.

**Account scoping is enforced in SQL, not in Python.** The visibility
predicate is compiled into every query's WHERE clause, so a chunk belonging
to another customer's agreement is never loaded into the process at all —
there is no filtered-out object sitting in memory for a later bug to leak.
Two independent restrictions compose:

- `allowed_account_ids` — Phase 2's authorization hook. The caller may only
  ever see these accounts. `None` means unrestricted (no auth layer exists
  yet); an empty collection means no customer-specific document is visible.
- `account_id` — query scope. "I am asking about this customer", which
  additionally hides *other* customers' agreements even when the caller is
  authorized to see them.

General (non customer-specific) documents have `account_id IS NULL` and are
always visible under both restrictions — an account-scoped search still
returns the current policy and SOP, which is exactly what makes precedence
resolvable.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Sequence

from app.backend.models.documents import Document, Evidence

# Every column the Evidence model needs, flattened across the two tables.
_EVIDENCE_SELECT = """
    SELECT
        c.chunk_id, c.document_id, c.chunk_ordinal, c.page_number,
        c.section_number, c.section_title, c.subsection_title, c.section_path,
        c.topic, c.text, c.page_char_start, c.page_char_end,
        d.source_file, d.source_sha256, d.title AS document_title,
        d.document_type, d.status, d.status_raw,
        d.is_current, d.is_deprecated, d.is_authoritative, d.authority_tier,
        d.effective_date, d.updated_date, d.supersedes, d.superseded_by,
        d.account_id, d.customer_name
    FROM document_chunks c
    JOIN documents d ON d.document_id = c.document_id
"""


def visibility_sql(
    account_id: str | None,
    allowed_account_ids: Collection[str] | None,
    alias: str = "d",
) -> tuple[str, list[str]]:
    """Build the parameterized WHERE fragment implementing account scoping.

    Returned as (clause, params) so callers can AND it into any query. Params
    are ordered deterministically so identical scoping always produces an
    identical statement.
    """
    restrictions: list[str] = []
    params: list[str] = []

    if allowed_account_ids is not None:
        if not allowed_account_ids:
            # Authorized for no accounts at all: only general documents.
            return f"({alias}.account_id IS NULL)", []
        ordered = sorted(set(allowed_account_ids))
        placeholders = ",".join("?" * len(ordered))
        restrictions.append(f"{alias}.account_id IN ({placeholders})")
        params.extend(ordered)

    if account_id is not None:
        restrictions.append(f"{alias}.account_id = ?")
        params.append(account_id)

    if not restrictions:
        # Fully unscoped caller: general and customer-specific alike.
        return f"({alias}.account_id IS NULL OR {alias}.account_id IS NOT NULL)", []

    return f"({alias}.account_id IS NULL OR ({' AND '.join(restrictions)}))", params


def _row_to_document(row: sqlite3.Row) -> Document:
    return Document(
        document_id=row["document_id"],
        source_file=row["source_file"],
        source_sha256=row["source_sha256"],
        title=row["title"],
        document_type=row["document_type"],
        status=row["status"],
        status_raw=row["status_raw"],
        is_current=bool(row["is_current"]),
        is_deprecated=bool(row["is_deprecated"]),
        is_authoritative=bool(row["is_authoritative"]),
        authority_tier=row["authority_tier"],
        account_id=row["account_id"],
        customer_name=row["customer_name"],
        plan=row["plan"],
        effective_date_raw=row["effective_date_raw"],
        effective_date=row["effective_date"],
        updated_date_raw=row["updated_date_raw"],
        updated_date=row["updated_date"],
        term_raw=row["term_raw"],
        term_start=row["term_start"],
        term_end=row["term_end"],
        supersedes=row["supersedes"],
        superseded_by=row["superseded_by"],
        page_count=row["page_count"],
    )


def _row_to_evidence(row: sqlite3.Row, *, score: float | None = None) -> Evidence:
    return Evidence(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        text=row["text"],
        source_file=row["source_file"],
        source_sha256=row["source_sha256"],
        page_number=row["page_number"],
        section_number=row["section_number"],
        section_title=row["section_title"],
        subsection_title=row["subsection_title"],
        section_path=row["section_path"],
        page_char_start=row["page_char_start"],
        page_char_end=row["page_char_end"],
        document_title=row["document_title"],
        document_type=row["document_type"],
        status=row["status"],
        status_raw=row["status_raw"],
        is_current=bool(row["is_current"]),
        is_deprecated=bool(row["is_deprecated"]),
        is_authoritative=bool(row["is_authoritative"]),
        authority_tier=row["authority_tier"],
        topic=row["topic"],
        effective_date=row["effective_date"],
        updated_date=row["updated_date"],
        supersedes=row["supersedes"],
        superseded_by=row["superseded_by"],
        account_id=row["account_id"],
        customer_name=row["customer_name"],
        score=score,
    )


def list_documents(
    conn: sqlite3.Connection,
    *,
    account_id: str | None = None,
    allowed_account_ids: Collection[str] | None = None,
) -> list[Document]:
    clause, params = visibility_sql(account_id, allowed_account_ids)
    rows = conn.execute(
        f"SELECT * FROM documents d WHERE {clause} ORDER BY d.document_id", params
    ).fetchall()
    return [_row_to_document(r) for r in rows]


def get_document(
    conn: sqlite3.Connection,
    document_id: str,
    *,
    account_id: str | None = None,
    allowed_account_ids: Collection[str] | None = None,
) -> Document | None:
    """None means "not visible to you" — indistinguishable from "does not
    exist", so an out-of-scope caller cannot probe for another customer's
    agreement by watching which ids return an error."""
    clause, params = visibility_sql(account_id, allowed_account_ids)
    row = conn.execute(
        f"SELECT * FROM documents d WHERE d.document_id = ? AND {clause}",
        [document_id, *params],
    ).fetchone()
    return None if row is None else _row_to_document(row)


def get_document_chunks(
    conn: sqlite3.Connection,
    document_id: str,
    *,
    account_id: str | None = None,
    allowed_account_ids: Collection[str] | None = None,
) -> list[Evidence]:
    clause, params = visibility_sql(account_id, allowed_account_ids)
    rows = conn.execute(
        f"{_EVIDENCE_SELECT} WHERE c.document_id = ? AND {clause} ORDER BY c.chunk_ordinal",
        [document_id, *params],
    ).fetchall()
    return [_row_to_evidence(r) for r in rows]


def get_evidence_by_chunk_ids(
    conn: sqlite3.Connection,
    chunk_ids: Sequence[str],
    *,
    account_id: str | None = None,
    allowed_account_ids: Collection[str] | None = None,
) -> list[Evidence]:
    """Re-fetch specific chunks by id. Out-of-scope or unknown ids are simply
    absent from the result; the caller gets no signal distinguishing them."""
    if not chunk_ids:
        return []
    clause, params = visibility_sql(account_id, allowed_account_ids)
    placeholders = ",".join("?" * len(chunk_ids))
    rows = conn.execute(
        f"{_EVIDENCE_SELECT} WHERE c.chunk_id IN ({placeholders}) AND {clause} "
        f"ORDER BY c.document_id, c.chunk_ordinal",
        [*chunk_ids, *params],
    ).fetchall()
    return [_row_to_evidence(r) for r in rows]


def fetch_searchable_evidence(
    conn: sqlite3.Connection,
    *,
    account_id: str | None = None,
    allowed_account_ids: Collection[str] | None = None,
    include_non_authoritative: bool = True,
) -> list[Evidence]:
    """Every chunk the caller is permitted to see, as scoring candidates.

    The corpus is a few dozen chunks, so the search layer ranks this list in
    Python rather than maintaining an index. Loading only *visible* chunks
    also means out-of-scope documents cannot influence corpus-wide scoring
    statistics — see app/backend/retrieval/search.py.
    """
    clause, params = visibility_sql(account_id, allowed_account_ids)
    sql = f"{_EVIDENCE_SELECT} WHERE {clause}"
    if not include_non_authoritative:
        sql += " AND d.is_authoritative = 1"
    sql += " ORDER BY c.chunk_id"
    return [_row_to_evidence(r) for r in conn.execute(sql, params).fetchall()]


def get_evidence_by_topic(
    conn: sqlite3.Connection,
    topic: str,
    *,
    account_id: str | None = None,
    allowed_account_ids: Collection[str] | None = None,
    include_non_authoritative: bool = False,
) -> list[Evidence]:
    """Every visible chunk on one subject-matter topic.

    The policy engine uses this rather than BM25 search: a cancellation-fee
    calculation must not depend on how a question happened to be phrased.
    Topic membership is assigned deterministically at ingestion from section
    headings, so this returns the same clauses every time.

    Defaults to authoritative sources only — a policy computation should not
    even see deprecated terms as candidates.
    """
    clause, params = visibility_sql(account_id, allowed_account_ids)
    sql = f"{_EVIDENCE_SELECT} WHERE c.topic = ? AND {clause}"
    if not include_non_authoritative:
        sql += " AND d.is_authoritative = 1"
    sql += " ORDER BY d.authority_tier, c.chunk_id"
    rows = conn.execute(sql, [topic, *params]).fetchall()
    return [_row_to_evidence(r) for r in rows]


def get_latest_document_ingestion_run(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM document_ingestion_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return None if row is None else dict(row)
