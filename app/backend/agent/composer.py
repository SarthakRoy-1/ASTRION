"""Compose an answer strictly from tool results.

Used when the planning provider returns no prose of its own — which is the
case for `DeterministicPlanner`, and is also the safe fallback for any
provider. Every sentence produced here is assembled from a field of a typed
tool result: a policy decision's `controlling_rule` and `calculation`, a
citation, an explicit verification reason.

Nothing is inferred, softened, or filled in. If a decision says
REQUIRES_VERIFICATION, the text says so and lists what is missing rather than
choosing the likely answer — an unverified service credit becomes a real
refund to a real customer.
"""

from __future__ import annotations

from app.backend.agent.provider import Intent, StepRecord
from app.backend.models.agent import ResponseOutcome, ToolStatus
from app.backend.models.policy import (
    CancellationDecision,
    PolicyOutcome,
    ServiceCreditDecision,
)

# A policy question needs an order to evaluate. If one was asked and none was
# resolved, that gap is the answer — not a pile of related documentation.
_POLICY_INTENT_REQUIREMENTS: tuple[tuple[Intent, type, str], ...] = (
    (Intent.CANCELLATION, CancellationDecision, "whether a cancellation fee applies"),
    (Intent.SERVICE_CREDIT, ServiceCreditDecision, "service-credit eligibility"),
)


def compose(
    message: str, history: list[StepRecord], intents: set[Intent] | None = None
) -> tuple[str, ResponseOutcome, list[str]]:
    """Return (answer, outcome, uncertainties)."""
    lines: list[str] = []
    uncertainties: list[str] = []
    intents = intents or set()

    decisions = [d for step in history for d in step.result.decisions]
    proposals = [
        step.result.proposed_action for step in history if step.result.proposed_action
    ]
    unmet_intents = _unmet_policy_intents(intents, decisions, history)

    for decision in decisions:
        if isinstance(decision, CancellationDecision):
            lines.extend(_cancellation_lines(decision))
        elif isinstance(decision, ServiceCreditDecision):
            lines.extend(_service_credit_lines(decision))
        uncertainties.extend(decision.verification_reasons)
        for override in decision.overrides:
            lines.append(f"Precedence: {override}")

    # Surface retrieval and lookup failures rather than answering around them.
    for step in history:
        result = step.result
        if result.status in (ToolStatus.NOT_FOUND, ToolStatus.FORBIDDEN) and result.message:
            uncertainties.append(result.message)
            lines.append(f"Could not complete `{step.tool_name}`: {result.message}")
        elif result.status is ToolStatus.ERROR and result.message:
            uncertainties.append(result.message)
            lines.append(f"`{step.tool_name}` failed: {result.message}")

    conflicts = [
        conflict
        for step in history
        for conflict in step.result.data.get("conflicts", [])
        if isinstance(step.result.data.get("conflicts"), list)
    ]
    for conflict in conflicts:
        lines.append(f"Unresolved source conflict: {conflict}")
        uncertainties.append(conflict)

    # A ticket's historical_resolution is flagged non-authoritative at the tool
    # layer (record_tools._lookup_record), but that flag only protects an
    # answer that a *decision* was computed for — it does nothing for a bare
    # documentation lookup, which is exactly the shape of "does this old
    # ticket's guidance still apply?" A caller reading only the composed
    # answer would otherwise never learn the flag existed.
    for note in _historical_resolution_notes(history):
        lines.append(note)
        uncertainties.append(note)

    # An unanswerable policy question is reported as such, before any
    # documentation summary, so it cannot read as though it were answered.
    for description in unmet_intents:
        gap = (
            f"I cannot determine {description} without knowing which order this concerns. "
            f"Provide the order id (for example ORD-1001) and I will evaluate it against "
            f"the governing policy and any applicable customer agreement."
        )
        lines.append(gap)
        uncertainties.append(f"no order identified, so {description} was not evaluated")

    if proposals:
        proposed = proposals[-1]
        lines.append(
            f"Prepared action (NOT yet performed): {proposed.preview} "
            f"Confirm action {proposed.action_id} to execute it."
        )

    if not lines:
        governing = _governing_evidence(history)
        if governing:
            # Name the governing sections rather than counting them. Without
            # a policy decision or an action to report, the citations *are*
            # the answer, and a bare count leaves the caller to re-derive it.
            lines.append(
                "The governing documentation for this question is below. "
                "Read the cited sections; nothing here has been summarised or "
                "interpreted beyond what they state."
            )
            lines.extend(f"Source: {item.citation}" for item in governing)
        elif any(step.result.evidence for step in history):
            lines.append(
                "Only non-authoritative material matched this question — see the "
                "evidence list, and treat it as context rather than as policy."
            )
            uncertainties.append("no authoritative source matched this question")
        else:
            lines.append(
                "I could not find enough information in the supplied sources to answer that."
            )
            uncertainties.append("no applicable records or document evidence were found")

    outcome = _outcome(decisions, uncertainties, bool(proposals), unmet_intents)
    if outcome is ResponseOutcome.UNCERTAIN:
        lines.append(
            "This cannot be answered definitively from the available data — "
            "verify the points listed before committing to an outcome."
        )

    return "\n".join(lines), outcome, _dedupe(uncertainties)


def _cancellation_lines(decision: CancellationDecision) -> list[str]:
    lines: list[str] = []
    if decision.can_cancel:
        if decision.fee_applies:
            lines.append(
                f"Order {decision.order_id} can be cancelled, and a cancellation fee of "
                f"{decision.currency} {decision.fee_amount} applies."
            )
        elif decision.fee_amount is not None:
            lines.append(
                f"Order {decision.order_id} can be cancelled with no cancellation fee."
            )
        else:
            lines.append(
                f"Order {decision.order_id} can be cancelled, but the fee could not be "
                f"determined."
            )
    else:
        lines.append(f"Order {decision.order_id} cannot be cancelled.")
        if decision.alternative_workflow:
            lines.append(f"Use the {decision.alternative_workflow} workflow instead.")

    lines.append(f"Rule applied: {decision.controlling_rule}")
    if decision.calculation:
        lines.append(f"Calculation: {decision.calculation}")
    lines.extend(_source_lines(decision.controlling_sources))
    return lines


def _service_credit_lines(decision: ServiceCreditDecision) -> list[str]:
    lines: list[str] = []
    if decision.outcome is PolicyOutcome.ELIGIBLE:
        lines.append(
            f"Order {decision.order_id} qualifies for a failed-pickup service credit of "
            f"{decision.currency} {decision.credit_amount}."
        )
    elif decision.outcome is PolicyOutcome.REQUIRES_VERIFICATION:
        if decision.credit_amount is not None:
            lines.append(
                f"Order {decision.order_id} would qualify for a service credit of "
                f"{decision.currency} {decision.credit_amount}, but this is provisional "
                f"and must not be promised to the customer yet."
            )
        else:
            lines.append(
                f"Service-credit eligibility for order {decision.order_id} cannot be "
                f"determined from the available data."
            )
    else:
        lines.append(
            f"Order {decision.order_id} does not qualify for a failed-pickup service credit."
        )

    if decision.delay_hours is not None and decision.threshold_hours is not None:
        observed = "confirmed pickup" if decision.pickup_confirmed else "dataset snapshot"
        lines.append(
            f"Measured {decision.delay_hours}h past the scheduled pickup-window end "
            f"(against the {observed}) versus a {decision.threshold_hours}h threshold; "
            f"carrier fault: {decision.carrier_fault}, customer fault: "
            f"{decision.customer_fault}."
        )
    lines.append(f"Rule applied: {decision.controlling_rule}")
    if decision.calculation:
        lines.append(f"Calculation: {decision.calculation}")
    if decision.requires_manager_approval:
        lines.append("This credit exceeds the approval threshold and requires manager sign-off.")
    if decision.monthly_cap is not None:
        lines.append(
            f"A monthly aggregate cap of {decision.currency} {decision.monthly_cap} applies "
            f"to this account; check credits already issued this month."
        )
    lines.extend(_source_lines(decision.controlling_sources))
    return lines


def _source_lines(sources: list[str]) -> list[str]:
    return [f"Source: {source}" for source in sources]


def _governing_evidence(history: list[StepRecord]) -> list:
    """Authoritative evidence the retrieval layer marked as governing.

    Read from the tool result's own `governing` split — the authority layer
    already decided this, and re-deciding it here would create a second,
    competing precedence implementation.
    """
    governing_ids: list[str] = []
    for step in history:
        for entry in step.result.data.get("governing") or []:
            if isinstance(entry, dict) and entry.get("chunk_id"):
                governing_ids.append(entry["chunk_id"])

    seen: set[str] = set()
    items = []
    for step in history:
        for item in step.result.evidence:
            if item.chunk_id in governing_ids and item.chunk_id not in seen:
                seen.add(item.chunk_id)
                items.append(item)
    return items


def _unmet_policy_intents(
    intents: set[Intent], decisions: list, history: list[StepRecord]
) -> list[str]:
    """Policy questions that were asked but could not be evaluated.

    Only reported when no order was resolved at all. When an order *was*
    named but the lookup failed, the not-found line already explains it, and
    repeating the gap here would double-report one problem.
    """
    resolved_any_order = any(
        step.tool_name == "lookup_record"
        and step.result.ok
        and step.result.data.get("entity") == "order"
        for step in history
    )
    if resolved_any_order:
        return []

    lookup_failed = any(
        step.tool_name == "lookup_record" and not step.result.ok for step in history
    )
    if lookup_failed:
        return []

    return [
        description
        for intent, decision_type, description in _POLICY_INTENT_REQUIREMENTS
        if intent in intents
        and not any(isinstance(d, decision_type) for d in decisions)
    ]


def _outcome(
    decisions: list,
    uncertainties: list[str],
    has_proposal: bool,
    unmet_intents: list[str],
) -> ResponseOutcome:
    if has_proposal:
        # A prepared action dominates: the caller's next move is to confirm or
        # reject, even if some detail remains uncertain.
        return ResponseOutcome.NEEDS_CONFIRMATION
    if unmet_intents:
        return ResponseOutcome.UNCERTAIN
    if any(d.outcome is PolicyOutcome.REQUIRES_VERIFICATION for d in decisions):
        return ResponseOutcome.UNCERTAIN
    if uncertainties and not decisions:
        return ResponseOutcome.UNCERTAIN
    return ResponseOutcome.ANSWERED


def _dedupe(values: list[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


def _historical_resolution_notes(history: list[StepRecord]) -> list[str]:
    """One caution line per resolved ticket that carries a historical resolution.

    Reads exactly the fields `_lookup_record` already computed — the resolution
    text and its `historical_resolution_warning` — so this adds no judgment of
    its own about which tickets are suspect. A ticket without a historical
    resolution contributes nothing.
    """
    notes: list[str] = []
    for step in history:
        if step.tool_name != "lookup_record":
            continue
        record = step.result.data.get("record")
        if not isinstance(record, dict):
            continue
        resolution = record.get("historical_resolution")
        warning = record.get("historical_resolution_warning")
        if resolution and warning:
            ticket_id = record.get("ticket_id", "this ticket")
            notes.append(f'{ticket_id} historical note: {warning} On file: "{resolution}"')
    return notes
