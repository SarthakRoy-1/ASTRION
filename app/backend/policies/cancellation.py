"""Deterministic order-cancellation decisions (Phase 4).

Answers "may this order be cancelled, and at what fee" from structured order
facts plus the cancellation clauses that apply to that order's account. The
rule and the arithmetic are both code; the model's only role is deciding to
ask and explaining the result.

No customer is named anywhere in this module. An agreement that waives the
cancellation fee wins because `terms.py` recovered a waiver from the text of
whichever agreement Phase 3's authority layer scoped to this account — not
because of a branch on an account id.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection
from decimal import Decimal

from app.backend.models.documents import Topic
from app.backend.models.policy import (
    CancellationDecision,
    PolicyEvaluationContext,
    PolicyOutcome,
)
from app.backend.policies.base import (
    PolicyLookupError,
    citations,
    gather_policy_evidence,
    load_evaluation_context,
    minutes_between,
    money,
)
from app.backend.policies.terms import (
    extract_cancellation_terms,
    extract_pickup_confirmation_lag,
)
from app.backend.services.documents import get_evidence_by_topic
from app.backend.services.records import get_order

# Order states the SOP addresses explicitly. A status outside this set is not
# assumed to behave like any of them.
_DRAFT = "DRAFT"
_BOOKED = "BOOKED"
_PICKED_UP = "PICKED_UP"
_DELIVERED = "DELIVERED"


def evaluate_cancellation(
    conn: sqlite3.Connection,
    order_id: str,
    *,
    allowed_account_ids: Collection[str] | None = None,
    evaluation_context: PolicyEvaluationContext | None = None,
) -> CancellationDecision:
    """Decide whether `order_id` can be cancelled and what fee applies.

    Raises PolicyLookupError if the order does not exist or is outside the
    caller's scope — the two are indistinguishable by design.
    """
    order = get_order(conn, order_id, allowed_account_ids=allowed_account_ids)
    if order is None:
        raise PolicyLookupError(f"order {order_id!r} not found or not in scope")

    context = evaluation_context or load_evaluation_context(conn)
    evidence, authority = gather_policy_evidence(
        conn,
        topic=Topic.CANCELLATION,
        account_id=order.account_id,
        allowed_account_ids=allowed_account_ids,
    )
    terms = extract_cancellation_terms(evidence)
    overrides = [note.reason for note in authority.overrides]
    sources = citations(authority.governing) or citations(evidence)
    evidence_ids = [item.chunk_id for item in authority.governing] or [
        item.chunk_id for item in evidence
    ]

    status = (order.status or "").upper()
    requested_at = order.cancellation_requested_at or context.reference_time
    inputs: dict[str, str | None] = {
        "order_status": order.status,
        "booked_at": order.booked_at.isoformat() if order.booked_at else None,
        "cancellation_requested_at": (
            order.cancellation_requested_at.isoformat()
            if order.cancellation_requested_at
            else None
        ),
        "evaluated_against": requested_at.isoformat(),
        "reference_time_source": (
            context.reference_time_source
            if order.cancellation_requested_at is None
            else "order.cancellation_requested_at"
        ),
        "pickup_actual_at": (
            order.pickup_actual_at.isoformat() if order.pickup_actual_at else None
        ),
    }

    def build(
        *,
        outcome: PolicyOutcome,
        can_cancel: bool,
        fee_applies: bool,
        fee_amount: Decimal | None,
        rule: str,
        calculation: str | None = None,
        alternative: str | None = None,
        verification: list[str] | None = None,
    ) -> CancellationDecision:
        return CancellationDecision(
            order_id=order.order_id,
            account_id=order.account_id,
            outcome=outcome,
            can_cancel=can_cancel,
            fee_applies=fee_applies,
            fee_amount=fee_amount,
            currency=context.currency,
            controlling_rule=rule,
            controlling_sources=sources,
            alternative_workflow=alternative,
            requires_verification=bool(verification),
            verification_reasons=verification or [],
            inputs=inputs,
            calculation=calculation,
            overrides=overrides,
            evidence_chunk_ids=evidence_ids,
            terms=terms,
        )

    # --- states the SOP settles without any arithmetic ----------------------

    if status == _DELIVERED:
        return build(
            outcome=PolicyOutcome.NOT_ALLOWED,
            can_cancel=False,
            fee_applies=False,
            fee_amount=None,
            rule="A DELIVERED order cannot be cancelled.",
        )

    if status == _PICKED_UP:
        return build(
            outcome=PolicyOutcome.NOT_ALLOWED,
            can_cancel=False,
            fee_applies=False,
            fee_amount=None,
            rule="A PICKED_UP order must not be cancelled; the return-to-origin workflow applies.",
            alternative="return-to-origin",
        )

    if status == _DRAFT:
        return build(
            outcome=PolicyOutcome.ALLOWED,
            can_cancel=True,
            fee_applies=False,
            fee_amount=money(Decimal(0)),
            rule="A DRAFT order may be cancelled with no fee.",
        )

    if status != _BOOKED:
        return build(
            outcome=PolicyOutcome.REQUIRES_VERIFICATION,
            can_cancel=False,
            fee_applies=False,
            fee_amount=None,
            rule=f"Order status {order.status!r} is not one the cancellation SOP describes.",
            verification=[
                f"Order status {order.status!r} has no cancellation rule in the current SOP; "
                f"confirm the order state before acting."
            ],
        )

    # --- BOOKED: the only state where a fee can arise -----------------------

    verification: list[str] = []

    # A BOOKED order whose pickup window has already closed may simply be
    # showing a stale status — the product documentation warns that pickup
    # confirmation can lag. Cancelling a parcel that was in fact collected is
    # the mistake this guards against, and unlike a credit decision it is a
    # mistake that is made *sooner* rather than later, so the documented lag
    # window is live here.
    if (
        order.pickup_actual_at is None
        and order.pickup_window_end is not None
        and context.reference_time > order.pickup_window_end
    ):
        elapsed_since_window = minutes_between(
            order.pickup_window_end, context.reference_time
        )
        lag = extract_pickup_confirmation_lag(
            get_evidence_by_topic(
                conn,
                Topic.PRODUCT_KNOWN_ISSUES.value,
                account_id=order.account_id,
                allowed_account_ids=allowed_account_ids,
            ),
            order.carrier,
        )
        if lag is not None and elapsed_since_window <= Decimal(lag.lag_minutes):
            # Squarely inside the window the documentation describes: a BOOKED
            # status here is not evidence that collection did not happen.
            verification.append(
                f"Order is still BOOKED {elapsed_since_window} minutes past its pickup "
                f"window, which is within the documented {lag.lag_minutes}-minute "
                f"{lag.carrier} pickup-confirmation lag ({lag.source.source_file}). A "
                f"BOOKED status in this window is not evidence that the pickup did not "
                f"happen; confirm carrier status before cancelling."
            )
        else:
            verification.append(
                "Order is still BOOKED although its pickup window has closed and no pickup "
                "has been confirmed; verify carrier pickup status before cancelling."
            )

    if order.pickup_actual_at is not None:
        verification.append(
            "Order status is BOOKED but a pickup timestamp is recorded; the data conflicts "
            "and must be verified before a state-changing action."
        )

    if terms.fee_waived:
        return build(
            outcome=PolicyOutcome.ALLOWED,
            can_cancel=True,
            fee_applies=False,
            fee_amount=money(Decimal(0)),
            rule=(
                "A signed customer agreement waives the cancellation fee for a BOOKED order "
                "before pickup, overriding the SOP's default fee."
            ),
            calculation="fee waived by customer agreement -> 0",
            verification=verification or None,
        )

    if order.booked_at is None:
        verification.append(
            "The order has no booking timestamp, so the free-cancellation window cannot be "
            "evaluated."
        )
        return build(
            outcome=PolicyOutcome.REQUIRES_VERIFICATION,
            can_cancel=True,
            fee_applies=False,
            fee_amount=None,
            rule="Cancellation is permitted before pickup, but the fee cannot be determined.",
            verification=verification,
        )

    if terms.free_window_minutes is None or terms.fee_amount is None:
        verification.append(
            "The cancellation fee terms could not be read from the governing documents; "
            "confirm the applicable fee manually."
        )
        return build(
            outcome=PolicyOutcome.REQUIRES_VERIFICATION,
            can_cancel=True,
            fee_applies=False,
            fee_amount=None,
            rule="Cancellation is permitted before pickup, but the fee terms are unavailable.",
            verification=verification,
        )

    elapsed = minutes_between(order.booked_at, requested_at)
    inputs["minutes_since_booking"] = str(elapsed)
    within_window = elapsed <= Decimal(terms.free_window_minutes)

    if within_window:
        return build(
            outcome=PolicyOutcome.ALLOWED,
            can_cancel=True,
            fee_applies=False,
            fee_amount=money(Decimal(0)),
            rule=(
                f"A BOOKED order may be cancelled with no fee within "
                f"{terms.free_window_minutes} minutes of booking."
            ),
            calculation=(
                f"{elapsed} minutes since booking <= {terms.free_window_minutes} "
                f"minute free window -> no fee"
            ),
            verification=verification or None,
        )

    return build(
        outcome=PolicyOutcome.ALLOWED,
        can_cancel=True,
        fee_applies=True,
        fee_amount=money(terms.fee_amount),
        rule=(
            f"A BOOKED order cancelled more than {terms.free_window_minutes} minutes after "
            f"booking incurs the standard cancellation fee, as no customer agreement waives it."
        ),
        calculation=(
            f"{elapsed} minutes since booking > {terms.free_window_minutes} minute free "
            f"window -> {context.currency} {money(terms.fee_amount)}"
        ),
        verification=verification or None,
    )
