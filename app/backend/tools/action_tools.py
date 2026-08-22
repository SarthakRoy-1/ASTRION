"""Tool D — state-changing action *preparation*.

Only preparation is exposed as a tool. Confirming and executing an action is
`AgentOrchestrator.confirm_action`, which no tool can reach and which the
model therefore cannot call — a natural-language request can never do more
than produce a proposal awaiting a human.

Preparation validates that the target is real and within the caller's scope
before writing a PENDING_CONFIRMATION row. It performs no operational change.
"""

from __future__ import annotations

import sqlite3

from app.backend.models.actions import ActionType
from app.backend.models.agent import AgentContext, ToolResult, ToolStatus
from app.backend.services.actions import ActionError, prepare_action
from app.backend.services.records import get_ticket
from app.backend.tools.base import ToolSpec, require_str

PREPARE_ESCALATION = "prepare_escalation"
PREPARE_TICKET_NOTE = "prepare_ticket_note"


def _prepare_for_ticket(
    conn: sqlite3.Connection,
    context: AgentContext,
    arguments: dict,
    *,
    action_type: ActionType,
    parameter_keys: tuple[str, ...],
) -> ToolResult:
    ticket_id, error = require_str(arguments, "ticket_id")
    if error is not None:
        return error

    ticket = get_ticket(conn, ticket_id, allowed_account_ids=context.scope())
    if ticket is None:
        return ToolResult(
            status=ToolStatus.NOT_FOUND,
            message=f"ticket {ticket_id!r} was not found within the caller's scope",
        )

    parameters: dict[str, str] = {}
    for key in parameter_keys:
        value = arguments.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            return ToolResult(
                status=ToolStatus.INVALID_INPUT,
                message=f"argument {key!r} must be a non-empty string",
            )
        parameters[key] = value.strip()

    evidence_chunk_ids = arguments.get("evidence_chunk_ids") or []
    if not isinstance(evidence_chunk_ids, list) or not all(
        isinstance(c, str) for c in evidence_chunk_ids
    ):
        return ToolResult(
            status=ToolStatus.INVALID_INPUT,
            message="evidence_chunk_ids must be a list of strings",
        )

    try:
        proposed = prepare_action(
            conn,
            action_type=action_type,
            target_type="ticket",
            target_id=ticket.ticket_id,
            parameters=parameters,
            requested_by=context.user_id,
            requested_by_role=context.role.value,
            account_id=ticket.account_id,
            session_id=context.session_id,
            evidence_chunk_ids=evidence_chunk_ids,
            reason=parameters.get("reason") or parameters.get("note"),
        )
    except ActionError as exc:
        return ToolResult(status=ToolStatus.INVALID_INPUT, message=str(exc))

    return ToolResult(
        status=ToolStatus.OK,
        proposed_action=proposed,
        message=(
            "Prepared and awaiting confirmation. Nothing has changed yet; the action "
            "executes only after explicit confirmation."
        ),
        data={
            "action_id": proposed.action_id,
            "status": proposed.status.value,
            "preview": proposed.preview,
            "expires_at_utc": proposed.expires_at_utc.isoformat(),
        },
    )


def _prepare_escalation(
    conn: sqlite3.Connection, context: AgentContext, arguments: dict
) -> ToolResult:
    return _prepare_for_ticket(
        conn,
        context,
        arguments,
        action_type=ActionType.CREATE_ESCALATION,
        parameter_keys=("reason", "severity"),
    )


def _prepare_ticket_note(
    conn: sqlite3.Connection, context: AgentContext, arguments: dict
) -> ToolResult:
    return _prepare_for_ticket(
        conn,
        context,
        arguments,
        action_type=ActionType.ADD_TICKET_NOTE,
        parameter_keys=("note",),
    )


PREPARE_ESCALATION_SPEC = ToolSpec(
    name=PREPARE_ESCALATION,
    description=(
        "Prepare an escalation against a ticket. This does NOT escalate anything: it "
        "returns a preview and an action_id awaiting explicit human confirmation. "
        "Cite the evidence that justifies escalating via evidence_chunk_ids."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticket_id": {"type": "string", "description": "e.g. TKT-501"},
            "reason": {"type": "string", "description": "Why escalation is warranted."},
            "severity": {"type": "string", "description": "Optional, e.g. P1."},
            "evidence_chunk_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["ticket_id", "reason"],
    },
    handler=_prepare_escalation,
    mutating=True,
)

PREPARE_TICKET_NOTE_SPEC = ToolSpec(
    name=PREPARE_TICKET_NOTE,
    description=(
        "Prepare an internal note to add to a ticket. This does NOT write the note: it "
        "returns a preview and an action_id awaiting explicit human confirmation."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticket_id": {"type": "string"},
            "note": {"type": "string"},
            "evidence_chunk_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["ticket_id", "note"],
    },
    handler=_prepare_ticket_note,
    mutating=True,
)
