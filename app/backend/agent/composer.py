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
    SlaDecision,
)

# A policy question needs an order to evaluate. If one was asked and none was
# resolved, that gap is the answer — not a pile of related documentation.
_POLICY_INTENT_REQUIREMENTS: tuple[tuple[Intent, type, str], ...] = (
    (Intent.CANCELLATION, CancellationDecision, "whether a cancellation fee applies"),
    (Intent.SERVICE_CREDIT, ServiceCreditDecision, "service-credit eligibility"),
)


def unmet_policy_requirements(
    intents: set[Intent] | None, history: list[StepRecord]
) -> list[str]:
    """Prerequisites a policy question needed and did not get.

    Exposed so the trust layer can read the same gap the composer reports in
    prose, rather than re-deriving it from the answer text. One computation,
    two consumers — a second implementation would drift the moment either
    changed.
    """
    decisions = [d for step in history for d in step.result.decisions]
    # Phrased exactly as the composer phrases the matching uncertainty, so the
    # answer text and the trust reasons use one vocabulary rather than two
    # descriptions of the same gap.
    return [
        f"no order identified, so {description} was not evaluated"
        for description in _unmet_policy_intents(intents or set(), decisions, history)
    ]


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
        elif isinstance(decision, SlaDecision):
            lines.extend(_sla_lines(decision))
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

    # Operational signals are the answer to an operations question, so they are
    # rendered before the fallback that would otherwise report only citations.
    lines.extend(_signal_lines(history))

    if proposals:
        proposed = proposals[-1]
        lines.append(
            f"Prepared action (NOT yet performed): {proposed.preview} "
            f"Confirm action {proposed.action_id} to execute it."
        )

    if not lines:
        # No decision, action or failure to report: the records that *were*
        # resolved are the answer. Stating them is not interpretation — every
        # field below was returned by `lookup_record` under the caller's own
        # scope, so a record that appears here is one this caller may read.
        record_lines = _record_lines(history)
        lines.extend(record_lines)

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
        elif not record_lines:
            # Only now is "nothing found" true. Saying it while a resolved
            # record sat unreported in the tool history would be a false
            # negative about data the caller is entitled to see.
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


def _signal_lines(history: list[StepRecord]) -> list[str]:
    """Render detected operational signals from tool results.

    Every value here was produced by a detector reading real records. Nothing
    is summarised into a judgement the detector did not make: the priority, the
    counts and the explanation are copied through, and a signal the detector
    marked unsettled keeps that mark.
    """
    lines: list[str] = []

    for step in history:
        data = step.result.data or {}

        # --- a ranked listing ---
        signals = data.get("signals")
        if isinstance(signals, list) and signals:
            lines.append(
                f"{data.get('total_detected', len(signals))} operational signal(s) "
                f"detected in this workspace, highest priority first."
            )
            for signal in signals:
                trust = signal.get("trust_status")
                caveat = "" if trust == "confident" else f" [{trust}]"
                lines.append(
                    f"- [{signal.get('severity')}] {signal.get('title')} "
                    f"(priority {signal.get('priority_score')}, "
                    f"{signal.get('affected_account_count')} account(s), "
                    f"id {signal.get('signal_id')}){caveat}"
                )
                if step_next := signal.get("recommended_next_step"):
                    lines.append(f"  Next: {step_next}")

        # --- one signal in detail ---
        signal = data.get("signal")
        if isinstance(signal, dict):
            lines.append(f"{signal.get('title')}")
            lines.append(f"Why it was detected: {signal.get('detail')}")
            lines.append(
                f"Severity {signal.get('severity')}, priority "
                f"{signal.get('priority_score')}, affecting "
                f"{signal.get('affected_account_count')} account(s), "
                f"{signal.get('affected_ticket_count')} ticket(s) and "
                f"{signal.get('affected_order_count')} order(s)."
            )
            for factor in signal.get("priority_factors", []) or []:
                lines.append(
                    f"  Priority factor: {factor.get('name')} "
                    f"{factor.get('points'):+d} — {factor.get('basis')}"
                )
            records = signal.get("records") or []
            if records:
                lines.append(
                    "Records: "
                    + ", ".join(
                        f"{r.get('kind')} {r.get('id')}" for r in records[:10]
                    )
                )
            if next_step := signal.get("recommended_next_step"):
                lines.append(f"Recommended next step: {next_step}")
            # A recommendation is advice, never an instruction that anything
            # acts on. Saying so is what keeps a reader from assuming the
            # system has already started.
            lines.append(
                "This is a recommendation only. Nothing has been changed, and "
                "any action still requires explicit confirmation."
            )

    return lines


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


def _sla_lines(decision: SlaDecision) -> list[str]:
    lines: list[str] = []

    if decision.breached is True:
        lines.append(
            f"Ticket {decision.ticket_id} has BREACHED its {decision.severity} "
            f"first-response target of {decision.target_text}."
        )
    elif decision.breached is False:
        lines.append(
            f"Ticket {decision.ticket_id} is within its {decision.severity} "
            f"first-response target of {decision.target_text}."
        )
    elif decision.target_text is not None:
        lines.append(
            f"The {decision.severity} first-response target for ticket "
            f"{decision.ticket_id} is {decision.target_text}, but whether it has been "
            f"breached cannot be determined here."
        )
    else:
        lines.append(
            f"No first-response target has been applied to ticket "
            f"{decision.ticket_id}, because its severity is not established."
        )

    if decision.elapsed_minutes is not None:
        lines.append(
            f"{decision.elapsed_minutes} minutes have elapsed since the ticket was "
            f"created, measured against the dataset snapshot."
        )
    if decision.severity is not None and decision.severity_source:
        lines.append(f"Severity {decision.severity} — {decision.severity_source}.")

    lines.append(f"Rule applied: {decision.controlling_rule}")
    if decision.calculation:
        lines.append(f"Calculation: {decision.calculation}")
    if decision.requires_immediate_escalation:
        lines.append(
            "The current support policy requires P1 incidents to be escalated "
            "immediately, independently of the response-target arithmetic."
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


def _record_lines(history: list[StepRecord]) -> list[str]:
    """State the facts of each record `lookup_record` successfully returned.

    Reads only fields already present in the tool result, in a fixed order,
    with no inference: a missing field is reported as unrecorded rather than
    filled in. Scope was applied when the record was fetched, so anything
    reachable here is something this caller was permitted to read.
    """
    lines: list[str] = []
    for step in history:
        if step.tool_name != "lookup_record" or not step.result.ok:
            continue
        data = step.result.data
        entity = data.get("entity")
        record = data.get("record")

        if entity in ("account_orders", "account_tickets"):
            lines.extend(_collection_line(entity, data))
        elif isinstance(record, dict):
            renderer = _RECORD_RENDERERS.get(entity)
            if renderer is not None:
                lines.append(renderer(record))
    return _dedupe(lines)


def _collection_line(entity: str, data: dict) -> list[str]:
    records = data.get("records") or []
    if not records:
        return []
    account_id = data.get("account_id", "this account")
    if entity == "account_orders":
        listed = ", ".join(
            f"{r.get('order_id')} ({r.get('status')})" for r in records
        )
        return [f"Account {account_id} has {len(records)} order(s): {listed}."]
    listed = ", ".join(f"{r.get('ticket_id')} ({r.get('status')})" for r in records)
    return [f"Account {account_id} has {len(records)} ticket(s): {listed}."]


def _value(record: dict, key: str, default: str = "not recorded") -> str:
    value = record.get(key)
    if value is None or value == "":
        return default
    return str(value)


def _account_line(record: dict) -> str:
    return (
        f"Account {_value(record, 'account_id')} — {_value(record, 'account_name')}, "
        f"plan {_value(record, 'plan')}, status {_value(record, 'status')}, "
        f"CSM {_value(record, 'csm')}, premium support: "
        f"{_value(record, 'premium_support')}."
    )


def _order_line(record: dict) -> str:
    return (
        f"Order {_value(record, 'order_id')} (account "
        f"{_value(record, 'account_id')}): status {_value(record, 'status')}, "
        f"carrier {_value(record, 'carrier')}, booked {_value(record, 'booked_at')}, "
        f"pickup window {_value(record, 'pickup_window_start')} to "
        f"{_value(record, 'pickup_window_end')}, pickup actual "
        f"{_value(record, 'pickup_actual_at', 'none recorded')}, shipment fee "
        f"{_value(record, 'shipment_fee_inr')}, carrier fault "
        f"{_value(record, 'carrier_fault', 'unknown')}, customer fault "
        f"{_value(record, 'customer_fault', 'unknown')}."
    )


def _ticket_line(record: dict) -> str:
    return (
        f"Ticket {_value(record, 'ticket_id')} (account "
        f"{_value(record, 'account_id')}): status {_value(record, 'status')}, "
        f"opened {_value(record, 'created_at')}, subject "
        f"\"{_value(record, 'subject')}\", channel {_value(record, 'channel')}, "
        f"assigned to {_value(record, 'assigned_to')}, last customer message "
        f"{_value(record, 'last_customer_message_at')}."
    )


def _metadata_line(record: dict) -> str:
    return (
        f"Dataset snapshot: {_value(record, 'dataset_snapshot')} — all time-based "
        f"reasoning is evaluated against this snapshot, not today's date."
    )


_RECORD_RENDERERS = {
    "account": _account_line,
    "order": _order_line,
    "ticket": _ticket_line,
    "dataset_metadata": _metadata_line,
}


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
