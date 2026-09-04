"""Tool E — operations intelligence.

Two read-only tools that let the agent answer "what should operations look at
right now?" from detected evidence rather than from the model's impression of
what is probably wrong.

The boundary is the same one every other tool holds, and is worth restating
because this tool returns aggregates rather than single records:

- **Scope is injected, never accepted.** `AgentContext.allowed_account_ids`
  reaches the detection layer, which compiles it into the WHERE clause. There
  is no argument that widens it, and `RESERVED_ARGUMENT_NAMES` in `tools/base`
  rejects any attempt to supply one.
- **The model cannot invent a signal.** Every field in the result comes from a
  detector that read real records. A signal the model wishes existed simply is
  not in the list.
- **Nothing here changes state.** Acting on a signal goes through the ordinary
  preparation tools and the confirmation gate; `recommended_next_step` is
  advisory prose and triggers nothing.
"""

from __future__ import annotations

import sqlite3

from app.backend.models.agent import AgentContext, ToolResult, ToolStatus
from app.backend.models.signals import Signal
from app.backend.operations.service import build_report, get_signal
from app.backend.tools.base import ToolSpec, require_str

GET_OPERATIONAL_SIGNALS = "get_operational_signals"
INVESTIGATE_SIGNAL = "investigate_signal"

#: How many signals a listing returns by default. Small on purpose: the tool
#: answers "what needs attention", and a hundred-item list answers nothing
#: while consuming the model's context.
DEFAULT_LIMIT = 8
MAX_LIMIT = 25


def _summarise(signal: Signal, *, full: bool) -> dict:
    """One signal as the model sees it.

    The listing form omits per-record detail and the ranking breakdown: they
    matter when investigating one signal and are noise across eight. The full
    form adds them, which is what `investigate_signal` is for.
    """
    summary: dict = {
        "signal_id": signal.signal_id,
        "signal_type": signal.signal_type.value,
        "severity": signal.severity.value,
        "priority_score": signal.priority_score,
        "title": signal.title,
        "affected_account_count": signal.affected_account_count,
        "affected_ticket_count": signal.affected_ticket_count,
        "affected_order_count": signal.affected_order_count,
        "trust_status": signal.trust_status,
        "recommended_next_step": signal.recommended_next_step,
    }
    if not full:
        return summary

    summary.update(
        {
            "detail": signal.detail,
            "affected_account_ids": list(signal.affected_account_ids),
            "records": [
                {
                    "kind": ref.kind.value,
                    "id": ref.record_id,
                    "account_id": ref.account_id,
                    "label": ref.label,
                }
                for ref in signal.record_refs
            ],
            "documentation_chunk_ids": list(signal.evidence_chunk_ids),
            "trust_reasons": list(signal.trust_reasons),
            # The ranking, itemised. The model is told *why* this is the
            # priority it is, so it can explain the ordering rather than
            # inventing a justification for a number.
            "priority_factors": [
                {"name": f.name, "points": f.points, "basis": f.basis}
                for f in signal.priority_factors
            ],
            "first_observed_at": (
                signal.first_observed_at.isoformat() if signal.first_observed_at else None
            ),
            "last_observed_at": (
                signal.last_observed_at.isoformat() if signal.last_observed_at else None
            ),
        }
    )
    return summary


def _get_operational_signals(
    conn: sqlite3.Connection, context: AgentContext, arguments: dict
) -> ToolResult:
    raw_limit = arguments.get("limit", DEFAULT_LIMIT)
    try:
        limit = max(1, min(int(raw_limit), MAX_LIMIT))
    except (TypeError, ValueError):
        return ToolResult(
            status=ToolStatus.INVALID_INPUT,
            message=f"limit must be a whole number between 1 and {MAX_LIMIT}",
        )

    wanted_type = arguments.get("signal_type")
    report = build_report(conn, allowed_account_ids=context.scope())

    signals = report.signals
    if wanted_type:
        signals = [s for s in signals if s.signal_type.value == str(wanted_type)]

    if not signals:
        return ToolResult(
            status=ToolStatus.NOT_FOUND,
            message=(
                "No operational signals were detected within this workspace's data"
                + (f" for type {wanted_type!r}" if wanted_type else "")
                + "."
            ),
            data={"signals": [], "count": 0},
        )

    return ToolResult(
        status=ToolStatus.OK,
        data={
            "count": len(signals[:limit]),
            "total_detected": len(report.signals),
            "reference_time": (
                report.reference_time.isoformat() if report.reference_time else None
            ),
            "signals": [_summarise(s, full=False) for s in signals[:limit]],
        },
        message=(
            f"{len(report.signals)} signal(s) detected, ranked by priority. "
            f"Every one is derived from records in this workspace; none is an "
            f"inference."
        ),
    )


def _investigate_signal(
    conn: sqlite3.Connection, context: AgentContext, arguments: dict
) -> ToolResult:
    signal_id, error = require_str(arguments, "signal_id")
    if error is not None:
        return error

    signal = get_signal(conn, signal_id, allowed_account_ids=context.scope())
    if signal is None:
        # Identical wording whether the signal is unknown or merely outside this
        # workspace — the same refusal shape the record layer uses, so this tool
        # cannot be used to discover that another tenant has a problem.
        return ToolResult(
            status=ToolStatus.NOT_FOUND,
            message=f"signal {signal_id!r} was not found within the caller's scope",
            data={"signal_id": signal_id},
        )

    # A signal the detector could not settle must not read as established fact.
    # Carrying its trust status onto the tool result is what makes the agent's
    # answer inherit the doubt rather than discard it.
    status = (
        ToolStatus.UNCERTAIN if signal.trust_status != "confident" else ToolStatus.OK
    )
    return ToolResult(
        status=status,
        data={"signal": _summarise(signal, full=True)},
        message=(
            signal.trust_reasons[0]
            if signal.trust_reasons
            else "Signal detail, derived from this workspace's records."
        ),
    )


GET_OPERATIONAL_SIGNALS_SPEC = ToolSpec(
    name=GET_OPERATIONAL_SIGNALS,
    description=(
        "List operational issues detected across this workspace's support and "
        "order data, ranked by priority: SLA risk, recurring issues, problems "
        "affecting several customers, and unusual operational patterns. Use this "
        "for questions like 'what should operations look at right now'. Every "
        "signal is derived from real records — you cannot infer one yourself."
    ),
    parameters={
        "type": "object",
        "properties": {
            "signal_type": {
                "type": "string",
                "enum": [
                    "sla_risk",
                    "recurring_issue",
                    "cross_customer_issue",
                    "operational_anomaly",
                ],
                "description": "Optional filter to one kind of signal.",
            },
            "limit": {
                "type": "integer",
                "description": f"How many to return, 1-{MAX_LIMIT}. Default {DEFAULT_LIMIT}.",
            },
        },
        "required": [],
    },
    handler=_get_operational_signals,
)

INVESTIGATE_SIGNAL_SPEC = ToolSpec(
    name=INVESTIGATE_SIGNAL,
    description=(
        "Get the full detail behind one operational signal: why it was detected, "
        "which tickets, orders and accounts it rests on, which documentation it "
        "matches, how its priority was calculated, and how far it can be trusted. "
        "Call this before explaining or acting on a signal."
    ),
    parameters={
        "type": "object",
        "properties": {
            "signal_id": {
                "type": "string",
                "description": "The signal_id from get_operational_signals.",
            }
        },
        "required": ["signal_id"],
    },
    handler=_investigate_signal,
)
