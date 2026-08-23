"""Deterministic failed-pickup service-credit decisions (Phase 4).

The SOP is explicit that this calculation must refuse to commit when its
inputs are unknown: "Do not promise a credit when carrier fault, pickup
timing, or customer fault is unknown." That instruction is implemented
literally here — an unknown input produces REQUIRES_VERIFICATION with a
*provisional* figure, never an eligibility promise.

That also covers the stale-pickup-status trap the product documentation
warns about: when no pickup has been confirmed, lateness is inferred from the
snapshot clock rather than observed, so the decision says so instead of
concluding the carrier failed to collect.

As in cancellation.py, no customer is named. A threshold or amount overridden
by an agreement wins because `terms.py` recovered it from that agreement's
text under Phase 3's authority scoping.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection
from decimal import Decimal

from app.backend.models.documents import Topic
from app.backend.models.policy import (
    PolicyEvaluationContext,
    PolicyOutcome,
    ServiceCreditDecision,
)
from app.backend.policies.base import (
    PolicyLookupError,
    citations,
    gather_policy_evidence,
    hours_between,
    load_evaluation_context,
    money,
)
from app.backend.policies.terms import (
    extract_pickup_confirmation_lag,
    extract_service_credit_terms,
)
from app.backend.services.documents import get_evidence_by_topic
from app.backend.services.records import get_order


def evaluate_service_credit(
    conn: sqlite3.Connection,
    order_id: str,
    *,
    allowed_account_ids: Collection[str] | None = None,
    evaluation_context: PolicyEvaluationContext | None = None,
) -> ServiceCreditDecision:
    """Decide failed-pickup service-credit eligibility for `order_id`."""
    order = get_order(conn, order_id, allowed_account_ids=allowed_account_ids)
    if order is None:
        raise PolicyLookupError(f"order {order_id!r} not found or not in scope")

    context = evaluation_context or load_evaluation_context(conn)
    evidence, authority = gather_policy_evidence(
        conn,
        topic=Topic.SERVICE_CREDIT,
        account_id=order.account_id,
        allowed_account_ids=allowed_account_ids,
    )
    terms = extract_service_credit_terms(evidence)

    # Known-issue text is fetched separately and never layered into `terms`:
    # a product known issue explains an operational symptom, it does not set
    # a commercial term. It is read here only to decide whether an absent
    # pickup confirmation is currently explainable.
    known_issues = get_evidence_by_topic(
        conn,
        Topic.PRODUCT_KNOWN_ISSUES.value,
        account_id=order.account_id,
        allowed_account_ids=allowed_account_ids,
    )
    lag = extract_pickup_confirmation_lag(known_issues, order.carrier)

    overrides = [note.reason for note in authority.overrides]
    sources = citations(authority.governing) or citations(evidence)
    evidence_ids = [item.chunk_id for item in authority.governing] or [
        item.chunk_id for item in evidence
    ]

    pickup_confirmed = order.pickup_actual_at is not None
    observed_at = order.pickup_actual_at or context.reference_time

    inputs: dict[str, str | None] = {
        "order_status": order.status,
        "pickup_window_end": (
            order.pickup_window_end.isoformat() if order.pickup_window_end else None
        ),
        "pickup_actual_at": (
            order.pickup_actual_at.isoformat() if order.pickup_actual_at else None
        ),
        "carrier_fault": None if order.carrier_fault is None else str(order.carrier_fault),
        "customer_fault": None if order.customer_fault is None else str(order.customer_fault),
        "shipment_fee": None if order.shipment_fee_inr is None else str(order.shipment_fee_inr),
        "evaluated_against": observed_at.isoformat(),
        "reference_time_source": (
            "order.pickup_actual_at" if pickup_confirmed else context.reference_time_source
        ),
        "carrier": order.carrier,
        "documented_pickup_confirmation_lag_minutes": (
            None if lag is None else str(lag.lag_minutes)
        ),
    }

    verification: list[str] = []

    def build(
        *,
        outcome: PolicyOutcome,
        eligible: bool,
        amount: Decimal | None,
        rule: str,
        delay: Decimal | None = None,
        calculation: str | None = None,
        provisional: bool = False,
        manager_approval: bool = False,
    ) -> ServiceCreditDecision:
        return ServiceCreditDecision(
            order_id=order.order_id,
            account_id=order.account_id,
            outcome=outcome,
            eligible=eligible,
            credit_amount=amount,
            provisional=provisional,
            currency=context.currency,
            delay_hours=delay,
            threshold_hours=terms.delay_threshold_hours,
            carrier_fault=order.carrier_fault,
            customer_fault=order.customer_fault,
            pickup_confirmed=pickup_confirmed,
            controlling_rule=rule,
            controlling_sources=sources,
            requires_verification=bool(verification),
            verification_reasons=verification,
            requires_manager_approval=manager_approval,
            monthly_cap=terms.monthly_cap,
            inputs=inputs,
            calculation=calculation,
            overrides=overrides,
            evidence_chunk_ids=evidence_ids,
            terms=terms,
        )

    if not terms.is_complete:
        verification.append(
            "The service-credit terms could not be read from the governing documents; "
            "confirm the applicable threshold and amount manually."
        )
        return build(
            outcome=PolicyOutcome.REQUIRES_VERIFICATION,
            eligible=False,
            amount=None,
            rule="Service-credit terms are unavailable for this account.",
        )

    if order.pickup_window_end is None:
        verification.append(
            "The order has no scheduled pickup-window end, so pickup lateness cannot be "
            "measured."
        )
        return build(
            outcome=PolicyOutcome.REQUIRES_VERIFICATION,
            eligible=False,
            amount=None,
            rule="Pickup timing is unknown, so no credit may be promised.",
        )

    delay = hours_between(order.pickup_window_end, observed_at)
    inputs["delay_hours"] = str(delay)

    # --- disqualifying facts, checked before any amount is computed --------
    #
    # Timing is tested first because it is the most objective input and
    # because reporting a fault-based reason for an order that is not yet
    # late would imply lateness had been established.

    if delay <= terms.delay_threshold_hours:
        return build(
            outcome=PolicyOutcome.NOT_ELIGIBLE,
            eligible=False,
            amount=None,
            rule=(
                f"A failed-pickup service credit requires the pickup to be more than "
                f"{terms.delay_threshold_hours} hours past the scheduled window end."
            ),
            delay=delay,
            calculation=(
                f"{delay}h past window end <= {terms.delay_threshold_hours}h threshold "
                f"-> not eligible"
            ),
        )

    if order.customer_fault is True:
        return build(
            outcome=PolicyOutcome.NOT_ELIGIBLE,
            eligible=False,
            amount=None,
            rule="A customer-caused issue disqualifies a failed-pickup service credit.",
            delay=delay,
        )

    if order.carrier_fault is False:
        return build(
            outcome=PolicyOutcome.NOT_ELIGIBLE,
            eligible=False,
            amount=None,
            rule="A failed-pickup service credit requires carrier fault, which is not recorded.",
            delay=delay,
        )

    # --- unknowns the SOP forbids resolving by assumption ------------------

    if order.carrier_fault is None:
        verification.append(
            "Carrier fault is unknown; the SOP forbids promising a credit until it is "
            "established."
        )
    if order.customer_fault is None:
        verification.append(
            "Customer fault is unknown; the SOP forbids promising a credit until it is "
            "established."
        )
    # An absent pickup confirmation is deliberately *not* a verification
    # reason of its own here.
    #
    # By this point the delay has already cleared the governing threshold, and
    # carrier fault is either recorded True — in which case the carrier has
    # accepted that the collection did not happen, and no webhook lag is
    # capable of contradicting that — or None, which the check above already
    # defers on. A documented confirmation lag is measured in minutes while
    # the credit thresholds are measured in hours, so it can never still be
    # the explanation at this stage.
    #
    # Blocking on it anyway would defer every failed-pickup credit the
    # governing agreement grants, which is why the lag is recorded as an audit
    # input rather than treated as doubt. The place a lag genuinely changes an
    # outcome is cancellation, where acting on a stale BOOKED status would
    # cancel a parcel that was in fact collected — see policies/cancellation.py.

    # --- amount -------------------------------------------------------------

    if terms.fixed_amount is not None:
        # A fixed amount stated by an agreement replaces the SOP's formula
        # outright; that is what "replaces the default ... credit amount"
        # means, so it is checked before the percentage calculation.
        amount = money(terms.fixed_amount)
        calculation = f"fixed credit -> {context.currency} {amount}"
    else:
        if order.shipment_fee_inr is None:
            verification.append(
                "The shipment fee is unknown, so the percentage-based credit cannot be "
                "calculated."
            )
            return build(
                outcome=PolicyOutcome.REQUIRES_VERIFICATION,
                eligible=False,
                amount=None,
                rule="The credit amount depends on a shipment fee that is not recorded.",
                delay=delay,
            )
        percentage_component = money(
            Decimal(str(order.shipment_fee_inr)) * terms.percentage_of_fee / Decimal(100)
        )
        amount = min(money(terms.max_amount), percentage_component)
        calculation = (
            f"lower of {context.currency} {money(terms.max_amount)} and "
            f"{terms.percentage_of_fee}% x {context.currency} "
            f"{money(Decimal(str(order.shipment_fee_inr)))} = {context.currency} "
            f"{percentage_component} -> {context.currency} {amount}"
        )

    needs_manager = (
        terms.manager_approval_above is not None and amount > terms.manager_approval_above
    )
    if needs_manager:
        calculation += (
            f"; exceeds {context.currency} {money(terms.manager_approval_above)} "
            f"and requires manager approval"
        )

    rule = (
        f"Pickup more than {terms.delay_threshold_hours} hours past the scheduled window "
        f"end with carrier fault and no customer fault qualifies for a service credit."
    )

    if verification:
        return build(
            outcome=PolicyOutcome.REQUIRES_VERIFICATION,
            eligible=False,
            amount=amount,
            provisional=True,
            rule=rule,
            delay=delay,
            calculation=calculation,
            manager_approval=needs_manager,
        )

    return build(
        outcome=PolicyOutcome.ELIGIBLE,
        eligible=True,
        amount=amount,
        rule=rule,
        delay=delay,
        calculation=calculation,
        manager_approval=needs_manager,
    )
