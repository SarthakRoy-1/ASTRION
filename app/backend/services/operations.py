"""Tenant-scoped aggregate reads over operational data.

Everything above this module works one record at a time — `get_ticket`,
`get_order`, `get_account_orders`. Detection needs the opposite: every open
ticket in scope, every order whose pickup window has passed, grouped and
counted. This module is that layer, and it follows exactly the rules
`services/records.py` established:

- **Parameterized SQL only.** No function here accepts a SQL string or fragment
  from a caller, so nothing above it can widen a query.
- **Scoping is compiled into the WHERE clause**, not filtered afterwards. An
  out-of-scope row is never loaded into the process at all, so there is no
  filtered-out object in memory for a later bug to leak.
- **`allowed_account_ids=None` means unrestricted**, matching the existing
  convention — used by scripts and by the policy engine's own tests, never by
  an HTTP caller, whose scope is always resolved from their workspace.

The one thing worth stating that is *not* obvious: an empty collection means
"authorized for no accounts", which must return nothing. That is a different
case from `None`, and conflating the two would turn a user with no workspace
into a user who can see everything.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection

from app.backend.models.records import Order, Ticket
from app.backend.services.records import _row_to_order, _row_to_ticket


def _scope_clause(
    allowed_account_ids: Collection[str] | None, alias: str = ""
) -> tuple[str, list[str]]:
    """The WHERE fragment implementing account scoping, and its parameters.

    Returned as (clause, params) so callers can AND it into any query, exactly
    as `services/documents.py::visibility_sql` does for the document layer.

    An empty collection compiles to a predicate that matches nothing rather
    than to no predicate at all — the difference between "authorized for no
    accounts" and "unrestricted".
    """
    prefix = f"{alias}." if alias else ""
    if allowed_account_ids is None:
        return "1 = 1", []
    ordered = sorted(set(allowed_account_ids))
    if not ordered:
        return "1 = 0", []
    placeholders = ",".join("?" * len(ordered))
    return f"{prefix}account_id IN ({placeholders})", ordered


# --- tickets ----------------------------------------------------------------


def list_tickets(
    conn: sqlite3.Connection,
    *,
    allowed_account_ids: Collection[str] | None = None,
    open_only: bool = False,
) -> list[Ticket]:
    """Every ticket in scope, oldest first.

    `open_only` filters on the status the workbook actually uses. Compared
    case-insensitively because a status is a human-entered label, and a
    detector that missed `Open` while matching `open` would silently under-
    report exactly the tickets that matter most.
    """
    clause, params = _scope_clause(allowed_account_ids)
    sql = f"SELECT * FROM tickets WHERE {clause}"
    if open_only:
        sql += " AND LOWER(COALESCE(status, '')) = 'open'"
    sql += " ORDER BY created_at, ticket_id"
    return [_row_to_ticket(row) for row in conn.execute(sql, params)]


def list_orders(
    conn: sqlite3.Connection,
    *,
    allowed_account_ids: Collection[str] | None = None,
) -> list[Order]:
    """Every order in scope, oldest first."""
    clause, params = _scope_clause(allowed_account_ids)
    return [
        _row_to_order(row)
        for row in conn.execute(
            f"SELECT * FROM orders WHERE {clause} ORDER BY booked_at, order_id", params
        )
    ]


def account_names(
    conn: sqlite3.Connection,
    *,
    allowed_account_ids: Collection[str] | None = None,
) -> dict[str, str]:
    """Account id -> display name, for labelling signals.

    Scoped like everything else: a name is customer data, and a detector that
    labelled an out-of-scope account would leak the one field most obviously
    identifying it.
    """
    clause, params = _scope_clause(allowed_account_ids)
    return {
        row["account_id"]: row["account_name"] or row["account_id"]
        for row in conn.execute(
            f"SELECT account_id, account_name FROM accounts WHERE {clause}", params
        )
    }


# --- aggregates -------------------------------------------------------------


def count_tickets_by_account(
    conn: sqlite3.Connection,
    *,
    allowed_account_ids: Collection[str] | None = None,
    open_only: bool = True,
) -> dict[str, int]:
    """Open ticket counts per account, for volume comparison."""
    clause, params = _scope_clause(allowed_account_ids)
    sql = f"SELECT account_id, COUNT(*) AS n FROM tickets WHERE {clause}"
    if open_only:
        sql += " AND LOWER(COALESCE(status, '')) = 'open'"
    sql += " GROUP BY account_id"
    return {row["account_id"]: int(row["n"]) for row in conn.execute(sql, params)}


def count_orders_by_carrier(
    conn: sqlite3.Connection,
    *,
    allowed_account_ids: Collection[str] | None = None,
) -> dict[str, dict[str, int]]:
    """Per carrier: how many orders, and across how many distinct accounts.

    The distinct-account count is what makes a carrier problem an *operations*
    concern rather than one customer's bad luck, so it is computed in SQL
    rather than inferred from a list the caller might have filtered.
    """
    clause, params = _scope_clause(allowed_account_ids)
    rows = conn.execute(
        f"""
        SELECT COALESCE(carrier, '(unknown)') AS carrier,
               COUNT(*) AS orders,
               COUNT(DISTINCT account_id) AS accounts,
               SUM(CASE WHEN carrier_fault = 1 THEN 1 ELSE 0 END) AS carrier_faults
          FROM orders
         WHERE {clause}
         GROUP BY COALESCE(carrier, '(unknown)')
        """,
        params,
    ).fetchall()
    return {
        row["carrier"]: {
            "orders": int(row["orders"]),
            "accounts": int(row["accounts"]),
            "carrier_faults": int(row["carrier_faults"] or 0),
        }
        for row in rows
    }
