"""Tool C — deterministic policy decisions.

The model may decide to call these and may explain what comes back. It does
not compute any of it. Every figure returned here was produced by
`app/backend/policies/`, which combines scoped structured facts with the
clauses Phase 3's authority layer says govern this account.

Each result carries the rule that controlled it, the citations behind that
rule, the inputs used, and the arithmetic — so the number can be checked
rather than trusted.
"""

from __future__ import annotations

import sqlite3

from app.backend.models.agent import AgentContext, ToolResult, ToolStatus
from app.backend.models.policy import PolicyOutcome
from app.backend.policies.base import PolicyDataError, PolicyLookupError
from app.backend.policies.cancellation import evaluate_cancellation
from app.backend.policies.service_credit import evaluate_service_credit
from app.backend.tools.base import ToolSpec, require_str

EVALUATE_CANCELLATION = "evaluate_cancellation"
EVALUATE_SERVICE_CREDIT = "evaluate_service_credit"


def _evaluate_cancellation(
    conn: sqlite3.Connection, context: AgentContext, arguments: dict
) -> ToolResult:
    order_id, error = require_str(arguments, "order_id")
    if error is not None:
        return error

    try:
        decision = evaluate_cancellation(
            conn, order_id, allowed_account_ids=context.scope()
        )
    except PolicyLookupError as exc:
        return ToolResult(status=ToolStatus.NOT_FOUND, message=str(exc))
    except PolicyDataError as exc:
        return ToolResult(status=ToolStatus.ERROR, message=str(exc))

    status = (
        ToolStatus.UNCERTAIN
        if decision.outcome is PolicyOutcome.REQUIRES_VERIFICATION
        else ToolStatus.OK
    )
    return ToolResult(
        status=status,
        decisions=[decision],
        message="; ".join(decision.verification_reasons) or None,
        data={"decision": decision.model_dump(mode="json")},
    )


def _evaluate_service_credit(
    conn: sqlite3.Connection, context: AgentContext, arguments: dict
) -> ToolResult:
    order_id, error = require_str(arguments, "order_id")
    if error is not None:
        return error

    try:
        decision = evaluate_service_credit(
            conn, order_id, allowed_account_ids=context.scope()
        )
    except PolicyLookupError as exc:
        return ToolResult(status=ToolStatus.NOT_FOUND, message=str(exc))
    except PolicyDataError as exc:
        return ToolResult(status=ToolStatus.ERROR, message=str(exc))

    status = (
        ToolStatus.UNCERTAIN
        if decision.outcome is PolicyOutcome.REQUIRES_VERIFICATION
        else ToolStatus.OK
    )
    return ToolResult(
        status=status,
        decisions=[decision],
        message="; ".join(decision.verification_reasons) or None,
        data={"decision": decision.model_dump(mode="json")},
    )


EVALUATE_CANCELLATION_SPEC = ToolSpec(
    name=EVALUATE_CANCELLATION,
    description=(
        "Determine whether an order may be cancelled and what cancellation fee applies. "
        "Applies the current SOP together with any customer agreement that governs this "
        "order's account, and returns the controlling rule, its citations, the inputs "
        "used and the arithmetic. Do not compute cancellation fees yourself; call this."
    ),
    parameters={
        "type": "object",
        "properties": {"order_id": {"type": "string", "description": "e.g. ORD-1001"}},
        "required": ["order_id"],
    },
    handler=_evaluate_cancellation,
)

EVALUATE_SERVICE_CREDIT_SPEC = ToolSpec(
    name=EVALUATE_SERVICE_CREDIT,
    description=(
        "Determine failed-pickup service-credit eligibility and amount for an order. "
        "Applies the current SOP together with any governing customer agreement. "
        "Returns requires_verification when carrier fault, customer fault or pickup "
        "timing is unknown — in that case no credit may be promised. Do not compute "
        "credit amounts yourself; call this."
    ),
    parameters={
        "type": "object",
        "properties": {"order_id": {"type": "string", "description": "e.g. ORD-2002"}},
        "required": ["order_id"],
    },
    handler=_evaluate_service_credit,
)
