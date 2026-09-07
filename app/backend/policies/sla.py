"""Deterministic first-response SLA decisions (Phase 7).

Answers "what is the response target for this ticket, and has it been missed"
from structured ticket facts plus the support-response clauses that apply to
that ticket's account. Like the other calculators in this package, the target
and the arithmetic are both code; the model's role is to decide to ask and to
explain what comes back.

Three things this module deliberately does *not* do:

- **It never names a customer, a plan, or a duration.** Targets are recovered
  from whichever documents the Phase 3 authority layer scoped to this account,
  so an agreement's negotiated target wins because the agreement said so, not
  because of a branch on an account id.

- **It never decides a severity.** Severity is a judgement about business
  impact — the kind of reading the policy's own definitions are written for a
  person to apply. When a caller supplies one it is used and recorded as
  caller-supplied. When none is supplied the module reports every target it
  read, together with the elapsed time, and returns REQUIRES_VERIFICATION.

  An earlier draft scored ticket text against the severity definitions and
  picked the best match. It was removed: on the supplied corpus a single
  shared word was enough to rate a billing question P1 and then announce a
  breach against a 15-minute target. A confident breach verdict resting on a
  guessed severity is precisely the failure this system exists to prevent, and
  a word-overlap score is not evidence.

- **It never uses wall-clock time.** Elapsed time is measured against the
  dataset snapshot, exactly as `cancellation.py` and `service_credit.py` do.

Business-hours targets are reported but not converted into a deadline: the
supplied corpus states business-hour targets ("2 business hours") without
defining a business calendar anywhere. Inventing one would invent the breach
verdict along with it, so those targets are surfaced with the calculation left
explicitly unresolved.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection
from decimal import Decimal

from app.backend.models.documents import Topic
from app.backend.models.policy import (
    PolicyEvaluationContext,
    PolicyOutcome,
    SlaDecision,
    Severity,
)
from app.backend.policies.base import (
    PolicyLookupError,
    citations,
    gather_policy_evidence,
    load_evaluation_context,
    minutes_between,
)
from app.backend.policies.terms import extract_response_targets
from app.backend.services.records import get_account, get_ticket


def evaluate_sla(
    conn: sqlite3.Connection,
    ticket_id: str,
    *,
    severity: str | None = None,
    allowed_account_ids: Collection[str] | None = None,
    evaluation_context: PolicyEvaluationContext | None = None,
) -> SlaDecision:
    """Decide the first-response target for `ticket_id` and whether it is breached."""
    ticket = get_ticket(conn, ticket_id, allowed_account_ids=allowed_account_ids)
    if ticket is None:
        raise PolicyLookupError(f"ticket {ticket_id!r} not found or not in scope")

    account = get_account(conn, ticket.account_id, allowed_account_ids=allowed_account_ids)
    plan = account.plan if account else None

    context = evaluation_context or load_evaluation_context(conn)
    evidence, authority = gather_policy_evidence(
        conn,
        topic=Topic.SUPPORT_RESPONSE,
        account_id=ticket.account_id,
        allowed_account_ids=allowed_account_ids,
    )
    targets = extract_response_targets(evidence, plan=plan)
    overrides = [note.reason for note in authority.overrides]
    sources = citations(authority.governing) or citations(evidence)
    evidence_ids = [item.chunk_id for item in authority.governing] or [
        item.chunk_id for item in evidence
    ]

    verification: list[str] = []

    # --- severity ---------------------------------------------------------
    #
    # An explicitly supplied severity is authoritative for this evaluation:
    # the caller looked at the ticket and made the call. Only when none is
    # supplied does the module try to derive one, and only from the policy's
    # own definitions.
    if severity is not None:
        resolved = Severity.parse(severity)
        if resolved is None:
            raise PolicyLookupError(
                f"severity {severity!r} is not one the current policy defines"
            )
        severity_source = "supplied by caller"
    else:
        resolved = None
        severity_source = "not supplied"
        available = ", ".join(
            f"{s.value} {t.text}" for s, t in sorted(targets.targets.items())
        )
        verification.append(
            "No severity was supplied, and severity is a judgement about business "
            "impact rather than a calculation, so no breach verdict is asserted here. "
            "Classify the ticket against the current policy's severity definitions and "
            "re-evaluate."
            + (f" Targets available for this account: {available}." if available else "")
        )

    inputs: dict[str, str | None] = {
        "ticket_status": ticket.status,
        "plan": plan,
        "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
        "first_response_at": None,
        "evaluated_against": context.reference_time.isoformat(),
        "reference_time_source": context.reference_time_source,
        "severity": None if resolved is None else resolved.value,
        "severity_source": severity_source,
    }

    def build(
        *,
        outcome: PolicyOutcome,
        breached: bool | None,
        rule: str,
        target: str | None = None,
        elapsed: Decimal | None = None,
        target_minutes: int | None = None,
        calculation: str | None = None,
        escalate: bool = False,
    ) -> SlaDecision:
        return SlaDecision(
            ticket_id=ticket.ticket_id,
            account_id=ticket.account_id,
            outcome=outcome,
            severity=resolved,
            severity_source=severity_source,
            plan=plan,
            target_text=target,
            target_minutes=target_minutes,
            elapsed_minutes=elapsed,
            breached=breached,
            first_response_recorded=False,
            requires_immediate_escalation=escalate,
            controlling_rule=rule,
            controlling_sources=sources,
            requires_verification=bool(verification),
            verification_reasons=verification,
            inputs=inputs,
            calculation=calculation,
            overrides=overrides,
            evidence_chunk_ids=evidence_ids,
            targets=targets,
        )

    # Elapsed time is a fact about the ticket and does not depend on severity,
    # so it is measured even when no target can be selected. An answer that
    # says "opened 30 minutes ago, targets are P1 15m / P2 1h" is useful;
    # withholding the clock until someone names a severity is not.
    elapsed: Decimal | None = None
    if ticket.created_at is not None:
        elapsed = minutes_between(ticket.created_at, context.reference_time)
        inputs["elapsed_minutes"] = str(elapsed)

    if resolved is None:
        return build(
            outcome=PolicyOutcome.REQUIRES_VERIFICATION,
            breached=None,
            elapsed=elapsed,
            rule="A first-response target cannot be selected without a severity.",
        )

    # P1 escalation is a standing instruction in the current policy, and it
    # does not wait on the breach arithmetic — a P1 is escalated immediately
    # whether or not its target has already elapsed.
    escalate_now = resolved is Severity.P1 and targets.escalate_p1_immediately

    target = targets.for_severity(resolved)
    if target is None:
        verification.append(
            f"No first-response target for {resolved.value} could be read from the "
            f"documents governing this account; confirm the applicable target manually."
        )
        return build(
            outcome=PolicyOutcome.REQUIRES_VERIFICATION,
            breached=None,
            rule=f"No {resolved.value} first-response target is available for this account.",
            escalate=escalate_now,
        )

    inputs["target_text"] = target.text

    if elapsed is None:
        verification.append(
            "The ticket has no creation timestamp, so elapsed time cannot be measured."
        )
        return build(
            outcome=PolicyOutcome.REQUIRES_VERIFICATION,
            breached=None,
            rule=f"The {resolved.value} first-response target is {target.text}.",
            target=target.text,
            escalate=escalate_now,
        )

    if target.minutes is None:
        # A business-hours target with no business calendar defined anywhere in
        # the corpus. Reporting the target is useful; asserting a breach from
        # it would be arithmetic on a unit this system has not been told how
        # to measure.
        verification.append(
            f"The applicable target is stated as {target.text!r}, in business hours. "
            f"The supplied documents do not define ASTRION's business calendar, so "
            f"elapsed business time — and therefore breach — cannot be computed here. "
            f"{elapsed} clock minutes have elapsed."
        )
        return build(
            outcome=PolicyOutcome.REQUIRES_VERIFICATION,
            breached=None,
            rule=f"The {resolved.value} first-response target is {target.text}.",
            target=target.text,
            elapsed=elapsed,
            escalate=escalate_now,
        )

    breached = elapsed > Decimal(target.minutes)
    calculation = (
        f"{elapsed} minutes elapsed since the ticket was created vs a "
        f"{target.minutes}-minute {resolved.value} target -> "
        f"{'breached' if breached else 'within target'}"
    )
    rule = (
        f"The {resolved.value} first-response target for this account is "
        f"{target.text}, and no first response is recorded against this ticket."
    )

    return build(
        outcome=PolicyOutcome.NOT_ALLOWED if breached else PolicyOutcome.ALLOWED,
        breached=breached,
        rule=rule,
        target=target.text,
        elapsed=elapsed,
        target_minutes=target.minutes,
        calculation=calculation,
        escalate=escalate_now,
    )
