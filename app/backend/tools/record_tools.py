"""Tool B — structured-data lookup.

A wrapper over the Phase 2 repository. Every read goes through
`app/backend/services/records.py`; there is no SQL in this module and no way
for a caller to supply any. The set of readable entities is fixed by the
`entity` enum in the schema below, so the reachable query surface is
enumerable rather than open-ended.

Scope comes from the execution context. Because the repository returns None
for out-of-scope records exactly as it does for missing ones, a caller cannot
use this tool to discover that another customer's record exists.
"""

from __future__ import annotations

import sqlite3

from app.backend.models.agent import AgentContext, ToolResult, ToolStatus
from app.backend.services.records import (
    get_account,
    get_account_orders,
    get_account_tickets,
    get_dataset_metadata,
    get_order,
    get_source_provenance,
    get_ticket,
)
from app.backend.tools.base import ToolSpec, require_str

LOOKUP_RECORD = "lookup_record"

_ENTITIES = ("account", "order", "ticket", "account_orders", "account_tickets", "dataset_metadata")

# Entities addressed by an id, and the argument that supplies it.
_ID_ARGUMENT = {
    "account": "account_id",
    "order": "order_id",
    "ticket": "ticket_id",
    "account_orders": "account_id",
    "account_tickets": "account_id",
}


def _lookup_record(
    conn: sqlite3.Connection, context: AgentContext, arguments: dict
) -> ToolResult:
    entity, error = require_str(arguments, "entity")
    if error is not None:
        return error
    if entity not in _ENTITIES:
        return ToolResult(
            status=ToolStatus.INVALID_INPUT,
            message=f"entity must be one of: {', '.join(_ENTITIES)}",
        )

    scope = context.scope()

    if entity == "dataset_metadata":
        metadata = get_dataset_metadata(conn)
        if metadata is None:
            return ToolResult(
                status=ToolStatus.NOT_FOUND,
                message="no dataset metadata; the workbook has not been ingested",
            )
        return ToolResult(
            status=ToolStatus.OK,
            data={
                "entity": entity,
                "record": {
                    "dataset_snapshot": metadata.dataset_snapshot_raw,
                    "dataset_snapshot_at": metadata.dataset_snapshot_at.isoformat(),
                    "timezone": metadata.dataset_timezone,
                    "currency": metadata.currency,
                    "important_note": metadata.important_note,
                    "source_workbook": metadata.source_workbook_filename,
                },
            },
        )

    record_id, error = require_str(arguments, _ID_ARGUMENT[entity])
    if error is not None:
        return error

    if entity == "account":
        account = get_account(conn, record_id, allowed_account_ids=scope)
        if account is None:
            return _not_found(entity, record_id)
        return _single(entity, account.model_dump(mode="json"))

    if entity == "order":
        order = get_order(conn, record_id, allowed_account_ids=scope)
        if order is None:
            return _not_found(entity, record_id)
        return _single(entity, order.model_dump(mode="json"), account_id=order.account_id)

    if entity == "ticket":
        ticket = get_ticket(conn, record_id, allowed_account_ids=scope)
        if ticket is None:
            return _not_found(entity, record_id)
        payload = ticket.model_dump(mode="json")
        if ticket.historical_resolution:
            # The workbook itself warns these may be wrong. Flagging it on the
            # way out keeps a past resolution from reading like current policy.
            payload["historical_resolution_warning"] = (
                "Historical resolutions are context only and may contain incorrect past "
                "guidance. Do not treat this as policy authority."
            )
        return _single(entity, payload, account_id=ticket.account_id)

    if entity == "account_orders":
        orders = get_account_orders(conn, record_id, allowed_account_ids=scope)
        if not orders:
            return _empty_collection(entity, record_id)
        return ToolResult(
            status=ToolStatus.OK,
            data={
                "entity": entity,
                "account_id": record_id,
                "count": len(orders),
                "records": [o.model_dump(mode="json") for o in orders],
            },
        )

    tickets = get_account_tickets(conn, record_id, allowed_account_ids=scope)
    if not tickets:
        return _empty_collection(entity, record_id)
    return ToolResult(
        status=ToolStatus.OK,
        data={
            "entity": entity,
            "account_id": record_id,
            "count": len(tickets),
            "records": [t.model_dump(mode="json") for t in tickets],
        },
    )


def _single(entity: str, record: dict, *, account_id: str | None = None) -> ToolResult:
    return ToolResult(
        status=ToolStatus.OK,
        data={"entity": entity, "account_id": account_id, "record": record},
    )


def _not_found(entity: str, record_id: str) -> ToolResult:
    # Identical message whether the record is missing or merely out of scope:
    # distinguishing them would confirm the existence of another customer's data.
    return ToolResult(
        status=ToolStatus.NOT_FOUND,
        message=f"{entity} {record_id!r} was not found within the caller's scope",
        data={"entity": entity, "id": record_id},
    )


def _empty_collection(entity: str, record_id: str) -> ToolResult:
    return ToolResult(
        status=ToolStatus.NOT_FOUND,
        message=f"no {entity} available for {record_id!r} within the caller's scope",
        data={"entity": entity, "account_id": record_id, "count": 0, "records": []},
    )


def _lookup_provenance(
    conn: sqlite3.Connection, context: AgentContext, arguments: dict
) -> ToolResult:
    table, error = require_str(arguments, "target_table")
    if error is not None:
        return error
    target_id, error = require_str(arguments, "target_id")
    if error is not None:
        return error

    # Provenance is only released for a record the caller can already read,
    # so this cannot become a side channel around record scoping.
    scope = context.scope()
    readable = {
        "accounts": lambda: get_account(conn, target_id, allowed_account_ids=scope),
        "orders": lambda: get_order(conn, target_id, allowed_account_ids=scope),
        "tickets": lambda: get_ticket(conn, target_id, allowed_account_ids=scope),
    }.get(table)
    if readable is None:
        return ToolResult(
            status=ToolStatus.INVALID_INPUT,
            message="target_table must be one of: accounts, orders, tickets",
        )
    if readable() is None:
        return _not_found(table, target_id)

    provenance = get_source_provenance(conn, table, target_id)
    if provenance is None:
        return ToolResult(
            status=ToolStatus.NOT_FOUND,
            message=f"no provenance recorded for {table} {target_id!r}",
        )
    return ToolResult(status=ToolStatus.OK, data=provenance.model_dump(mode="json"))


LOOKUP_RECORD_SPEC = ToolSpec(
    name=LOOKUP_RECORD,
    description=(
        "Look up ASTRION operational records: an account, order or ticket by id, "
        "all orders or tickets for an account, or the dataset snapshot metadata. "
        "Returns only records the caller is permitted to see."
    ),
    parameters={
        "type": "object",
        "properties": {
            "entity": {"type": "string", "enum": list(_ENTITIES)},
            "account_id": {"type": "string", "description": "e.g. ACCT-001"},
            "order_id": {"type": "string", "description": "e.g. ORD-1001"},
            "ticket_id": {"type": "string", "description": "e.g. TKT-501"},
        },
        "required": ["entity"],
    },
    handler=_lookup_record,
)

LOOKUP_PROVENANCE_SPEC = ToolSpec(
    name="lookup_record_provenance",
    description=(
        "Show where a structured record came from: source workbook, sheet, row number "
        "and the original pre-parsing cell values."
    ),
    parameters={
        "type": "object",
        "properties": {
            "target_table": {"type": "string", "enum": ["accounts", "orders", "tickets"]},
            "target_id": {"type": "string"},
        },
        "required": ["target_table", "target_id"],
    },
    handler=_lookup_provenance,
)
