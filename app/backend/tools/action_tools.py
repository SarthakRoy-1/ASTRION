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
from app.backend.models.policy import PolicyOutcome
from app.backend.policies.base import PolicyDataError, PolicyLookupError
from app.backend.policies.service_credit import evaluate_service_credit
from app.backend.services.actions import ActionError, prepare_action
from app.backend.services.records import get_ticket
from app.backend.tools.base import ToolSpec, require_str

PREPARE_ESCALATION = "prepare_escalation"
PREPARE_TICKET_NOTE = "prepare_ticket_note"
PREPARE_SERVICE_CREDIT = "prepare_service_credit"


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


def _prepare_service_credit(
    conn: sqlite3.Connection, context: AgentContext, arguments: dict
) -> ToolResult:
    """Prepare a service credit whose amount the caller cannot choose.

    Three things this deliberately refuses to do:

    - **Take an amount.** The figure comes from `evaluate_service_credit` and
      nowhere else. A model argument named `amount` is rejected rather than
      ignored, because silently dropping it would let a caller believe they
      had set one.
    - **Prepare an ineligible or unsettled credit.** An order the policy
      engine declines, or one it can only answer provisionally, produces an
      uncertainty result and no executable action. `REQUIRES_VERIFICATION`
      exists precisely so a missing fault determination does not become an
      invented payment.
    - **Decide who may approve it.** It records *whether* the SOP's threshold
      applies; who may confirm that is settled at confirmation time, under the
      confirming caller.
    """
    order_id, error = require_str(arguments, "order_id")
    if error is not None:
        return error

    # Rejected, not ignored. The whole point of this tool is that the amount is
    # not negotiable, and a caller who supplied one must be told it was refused.
    for forbidden in ("amount", "credit_amount", "currency"):
        if forbidden in arguments:
            return ToolResult(
                status=ToolStatus.INVALID_INPUT,
                message=(
                    f"argument {forbidden!r} is not accepted: a service credit's "
                    f"amount is computed from the governing agreement and policy, "
                    f"not supplied by the caller"
                ),
            )

    try:
        decision = evaluate_service_credit(
            conn, order_id, allowed_account_ids=context.scope()
        )
    except PolicyLookupError as exc:
        return ToolResult(status=ToolStatus.NOT_FOUND, message=str(exc))
    except PolicyDataError as exc:
        return ToolResult(status=ToolStatus.ERROR, message=str(exc))

    if decision.outcome is PolicyOutcome.REQUIRES_VERIFICATION or decision.provisional:
        return ToolResult(
            status=ToolStatus.UNCERTAIN,
            decisions=[decision],
            message=(
                "; ".join(decision.verification_reasons)
                or "the credit could not be settled from the available facts"
            ),
        )

    if not decision.eligible or decision.credit_amount is None:
        return ToolResult(
            status=ToolStatus.NO_EVIDENCE,
            decisions=[decision],
            message=(
                f"order {order_id} does not qualify for a service credit: "
                f"{decision.controlling_rule}"
            ),
        )

    evidence_chunk_ids = arguments.get("evidence_chunk_ids") or []
    if not isinstance(evidence_chunk_ids, list) or not all(
        isinstance(c, str) for c in evidence_chunk_ids
    ):
        return ToolResult(
            status=ToolStatus.INVALID_INPUT,
            message="evidence_chunk_ids must be a list of strings",
        )

    # The decision's own citations are the justification for the payment, so
    # they travel with the proposal whether or not the caller cited anything.
    evidence = sorted({*evidence_chunk_ids, *decision.evidence_chunk_ids})

    parameters = {
        "amount": str(decision.credit_amount),
        "currency": decision.currency,
        "requires_manager_approval": (
            "true" if decision.requires_manager_approval else "false"
        ),
    }

    try:
        proposed = prepare_action(
            conn,
            action_type=ActionType.ISSUE_SERVICE_CREDIT,
            target_type="order",
            target_id=decision.order_id,
            parameters=parameters,
            requested_by=context.user_id,
            requested_by_role=context.role.value,
            account_id=decision.account_id,
            session_id=context.session_id,
            evidence_chunk_ids=evidence,
            reason=decision.controlling_rule,
        )
    except ActionError as exc:
        return ToolResult(status=ToolStatus.INVALID_INPUT, message=str(exc))

    return ToolResult(
        status=ToolStatus.OK,
        proposed_action=proposed,
        decisions=[decision],
        message=(
            "Prepared and awaiting confirmation. Nothing has changed yet; the credit "
            "is issued only after explicit confirmation."
            + (
                " Confirming it requires manager approval, because it exceeds the "
                "threshold the SOP sets."
                if decision.requires_manager_approval
                else ""
            )
        ),
        data={
            "action_id": proposed.action_id,
            "status": proposed.status.value,
            "preview": proposed.preview,
            "expires_at_utc": proposed.expires_at_utc.isoformat(),
            "requires_manager_approval": parameters["requires_manager_approval"],
        },
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

PREPARE_SERVICE_CREDIT_SPEC = ToolSpec(
    name=PREPARE_SERVICE_CREDIT,
    description=(
        "Prepare a failed-pickup service credit for an order. This does NOT issue "
        "the credit: it returns a preview and an action_id awaiting explicit human "
        "confirmation. The amount is computed from the governing customer agreement "
        "and the current SOP — do not supply one, and do not state a figure that did "
        "not come back from this tool."
    ),
    parameters={
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "e.g. ORD-2002"},
            "evidence_chunk_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["order_id"],
    },
    handler=_prepare_service_credit,
    mutating=True,
)
