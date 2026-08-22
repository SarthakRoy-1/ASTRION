"""Phase 4: deterministic policy calculations.

Runs against the real ingested corpus. Assertions are on the *rule applied*
and the arithmetic, both of which are traceable to a cited clause — never on
a number chosen to make a test pass.

The customer-specific cases exist to prove the override mechanism is generic:
nothing in app/backend/policies/ branches on an account id, so Northstar's fee
waiver and LumenWorks' credit terms must arrive via evidence or not at all.
"""

from decimal import Decimal

import pytest

from app.backend.models.documents import Topic
from app.backend.models.policy import PolicyOutcome
from app.backend.policies.base import PolicyLookupError, gather_policy_evidence
from app.backend.policies.cancellation import evaluate_cancellation
from app.backend.policies.service_credit import evaluate_service_credit
from app.backend.policies.terms import (
    extract_cancellation_terms,
    extract_service_credit_terms,
)
from conftest import BEACON_ACCOUNT, LUMENWORKS_ACCOUNT, NORTHSTAR_ACCOUNT


def cancellation_terms_for(conn, account_id):
    evidence, _ = gather_policy_evidence(
        conn, topic=Topic.CANCELLATION, account_id=account_id, allowed_account_ids=None
    )
    return extract_cancellation_terms(evidence)


def credit_terms_for(conn, account_id):
    evidence, _ = gather_policy_evidence(
        conn, topic=Topic.SERVICE_CREDIT, account_id=account_id, allowed_account_ids=None
    )
    return extract_service_credit_terms(evidence)


# --- term extraction ------------------------------------------------------------


def test_sop_cancellation_defaults_are_extracted(conn):
    terms = cancellation_terms_for(conn, BEACON_ACCOUNT)

    assert terms.free_window_minutes == 30
    assert terms.fee_amount == Decimal("250")
    assert terms.fee_waived is False


def test_agreement_waiver_is_extracted_for_that_account(conn):
    terms = cancellation_terms_for(conn, NORTHSTAR_ACCOUNT)

    assert terms.fee_waived is True
    waiver = next(s for s in terms.sources if s.field == "fee_waived")
    assert waiver.source_file.endswith(".pdf")
    assert int(waiver.authority_tier) == 1


def test_an_agreement_declining_a_waiver_does_not_grant_one(conn):
    """LumenWorks' agreement says "No special cancellation-fee waiver applies."
    A naive negation match would read that as granting a waiver."""
    terms = cancellation_terms_for(conn, LUMENWORKS_ACCOUNT)

    assert terms.fee_waived is False
    assert terms.fee_amount == Decimal("250")


def test_sop_conditional_waiver_wording_does_not_grant_a_waiver(conn):
    """The SOP itself says a fee applies "unless a customer agreement
    explicitly waives" it — describing a possibility, not granting it."""
    terms = cancellation_terms_for(conn, BEACON_ACCOUNT)

    assert terms.fee_waived is False


def test_sop_service_credit_defaults_are_extracted(conn):
    terms = credit_terms_for(conn, BEACON_ACCOUNT)

    assert terms.delay_threshold_hours == Decimal("2")
    assert terms.max_amount == Decimal("500")
    assert terms.percentage_of_fee == Decimal("10")
    assert terms.fixed_amount is None


def test_agreement_replaces_threshold_and_amount(conn):
    terms = credit_terms_for(conn, LUMENWORKS_ACCOUNT)

    assert terms.delay_threshold_hours == Decimal("4")
    assert terms.fixed_amount == Decimal("300")


def test_agreement_adds_a_cap_while_inheriting_sop_defaults(conn):
    """Northstar's agreement sets only a monthly cap and defers to the SOP for
    everything else — layering must preserve the SOP's threshold and formula."""
    terms = credit_terms_for(conn, NORTHSTAR_ACCOUNT)

    assert terms.monthly_cap == Decimal("5000")
    assert terms.delay_threshold_hours == Decimal("2")
    assert terms.max_amount == Decimal("500")
    assert terms.fixed_amount is None


def test_manager_approval_threshold_is_extracted(conn):
    terms = credit_terms_for(conn, BEACON_ACCOUNT)

    assert terms.manager_approval_above == Decimal("1000")


def test_one_accounts_terms_do_not_leak_into_another(conn):
    northstar = cancellation_terms_for(conn, NORTHSTAR_ACCOUNT)
    lumenworks = cancellation_terms_for(conn, LUMENWORKS_ACCOUNT)
    beacon = cancellation_terms_for(conn, BEACON_ACCOUNT)

    assert northstar.fee_waived is True
    assert lumenworks.fee_waived is False
    assert beacon.fee_waived is False

    assert credit_terms_for(conn, LUMENWORKS_ACCOUNT).fixed_amount == Decimal("300")
    assert credit_terms_for(conn, NORTHSTAR_ACCOUNT).fixed_amount is None
    assert credit_terms_for(conn, BEACON_ACCOUNT).monthly_cap is None


def test_every_extracted_term_names_its_source(conn):
    for account in (NORTHSTAR_ACCOUNT, LUMENWORKS_ACCOUNT, BEACON_ACCOUNT):
        for terms in (cancellation_terms_for(conn, account), credit_terms_for(conn, account)):
            for source in terms.sources:
                assert source.chunk_id
                assert source.source_file
                assert source.matched_text


# --- cancellation decisions ---------------------------------------------------------


def test_agreement_waiver_overrides_the_sop_fee(conn):
    """ORD-1001 was cancelled 120 minutes after booking — well past the SOP's
    30-minute window — but its account's agreement waives the fee."""
    decision = evaluate_cancellation(conn, "ORD-1001")

    assert decision.outcome is PolicyOutcome.ALLOWED
    assert decision.can_cancel is True
    assert decision.fee_applies is False
    assert decision.fee_amount == Decimal("0.00")
    assert "agreement" in decision.controlling_rule.lower()
    assert decision.overrides


def test_fee_applies_after_the_window_without_a_waiver(conn):
    """ORD-2001: 75 minutes after booking, and its agreement grants no waiver."""
    decision = evaluate_cancellation(conn, "ORD-2001")

    assert decision.fee_applies is True
    assert decision.fee_amount == Decimal("250.00")
    assert "75.00 minutes" in decision.calculation


def test_no_fee_within_the_window(conn):
    """ORD-3001: 15 minutes after booking, no agreement in the pack."""
    decision = evaluate_cancellation(conn, "ORD-3001")

    assert decision.outcome is PolicyOutcome.ALLOWED
    assert decision.fee_applies is False
    assert decision.fee_amount == Decimal("0.00")


def test_picked_up_order_cannot_be_cancelled(conn):
    decision = evaluate_cancellation(conn, "ORD-1002")

    assert decision.outcome is PolicyOutcome.NOT_ALLOWED
    assert decision.can_cancel is False
    assert decision.alternative_workflow == "return-to-origin"


def test_delivered_order_cannot_be_cancelled(conn):
    decision = evaluate_cancellation(conn, "ORD-4001")

    assert decision.outcome is PolicyOutcome.NOT_ALLOWED
    assert decision.can_cancel is False
    assert decision.alternative_workflow is None


def test_stale_booked_status_triggers_verification(conn):
    """ORD-2002 is still BOOKED although its pickup window closed. Product
    documentation warns confirmation can lag, so cancelling without checking
    risks cancelling a parcel already collected."""
    decision = evaluate_cancellation(conn, "ORD-2002")

    assert decision.requires_verification is True
    assert any("pickup" in reason.lower() for reason in decision.verification_reasons)


def test_cancellation_decision_carries_provenance(conn):
    decision = evaluate_cancellation(conn, "ORD-1001")

    assert decision.controlling_sources
    assert decision.evidence_chunk_ids
    assert decision.inputs["order_status"] == "BOOKED"
    assert decision.terms is not None


def test_cancellation_is_deterministic(conn):
    first = evaluate_cancellation(conn, "ORD-2001")
    second = evaluate_cancellation(conn, "ORD-2001")

    assert first.fee_amount == second.fee_amount
    assert first.controlling_rule == second.controlling_rule
    assert first.calculation == second.calculation


def test_unknown_order_raises_lookup_error(conn):
    with pytest.raises(PolicyLookupError):
        evaluate_cancellation(conn, "ORD-NOPE")


def test_out_of_scope_order_is_indistinguishable_from_missing(conn):
    with pytest.raises(PolicyLookupError):
        evaluate_cancellation(conn, "ORD-2001", allowed_account_ids={NORTHSTAR_ACCOUNT})


# --- service-credit decisions ------------------------------------------------------------


def test_agreement_threshold_and_fixed_amount_are_applied(conn):
    """ORD-2002 is 4.5h late with carrier fault; its agreement sets a 4h
    threshold and a fixed amount, replacing the SOP's 2h / lower-of formula."""
    decision = evaluate_service_credit(conn, "ORD-2002")

    assert decision.threshold_hours == Decimal("4")
    assert decision.credit_amount == Decimal("300.00")
    assert decision.delay_hours == Decimal("4.50")
    assert decision.carrier_fault is True


def test_unconfirmed_pickup_forces_verification_not_a_promise(conn):
    """The SOP forbids promising a credit when pickup timing is unknown, and
    the product guide warns pickup confirmation can lag."""
    decision = evaluate_service_credit(conn, "ORD-2002")

    assert decision.outcome is PolicyOutcome.REQUIRES_VERIFICATION
    assert decision.eligible is False
    assert decision.provisional is True
    assert decision.pickup_confirmed is False
    assert any("verify" in r.lower() for r in decision.verification_reasons)


def test_order_not_yet_late_is_not_eligible(conn):
    decision = evaluate_service_credit(conn, "ORD-1001")

    assert decision.outcome is PolicyOutcome.NOT_ELIGIBLE
    assert decision.delay_hours < 0
    assert "more than" in decision.controlling_rule


def test_default_lower_of_calculation(conn, monkeypatch):
    """With no agreement in play the SOP formula applies: the lower of the cap
    and the percentage of the shipment fee."""
    from app.backend.policies import service_credit as module

    real_get_order = module.get_order

    def late_beacon_order(connection, order_id, **kwargs):
        order = real_get_order(connection, order_id, **kwargs)
        if order is None or order.order_id != "ORD-3001":
            return order
        # Beacon's ORD-3001 with a carrier-fault failed pickup: fee 1200, so
        # 10% = 120, below the INR 500 cap.
        return order.model_copy(
            update={
                "carrier_fault": True,
                "pickup_actual_at": order.pickup_window_end.replace(hour=23),
            }
        )

    monkeypatch.setattr(module, "get_order", late_beacon_order)
    decision = evaluate_service_credit(conn, "ORD-3001")

    assert decision.outcome is PolicyOutcome.ELIGIBLE
    assert decision.threshold_hours == Decimal("2")
    assert decision.credit_amount == Decimal("120.00")
    assert "lower of" in decision.calculation


def test_credit_capped_by_the_max_when_percentage_is_higher(conn, monkeypatch):
    from app.backend.policies import service_credit as module

    real_get_order = module.get_order

    def expensive_late_order(connection, order_id, **kwargs):
        order = real_get_order(connection, order_id, **kwargs)
        if order is None or order.order_id != "ORD-3001":
            return order
        return order.model_copy(
            update={
                "carrier_fault": True,
                "shipment_fee_inr": 90000.0,
                "pickup_actual_at": order.pickup_window_end.replace(hour=23),
            }
        )

    monkeypatch.setattr(module, "get_order", expensive_late_order)
    decision = evaluate_service_credit(conn, "ORD-3001")

    # 10% of 90000 = 9000, so the INR 500 cap binds.
    assert decision.credit_amount == Decimal("500.00")


def test_customer_fault_disqualifies(conn, monkeypatch):
    from app.backend.policies import service_credit as module

    real_get_order = module.get_order

    def customer_at_fault(connection, order_id, **kwargs):
        order = real_get_order(connection, order_id, **kwargs)
        if order is None or order.order_id != "ORD-2002":
            return order
        return order.model_copy(update={"customer_fault": True})

    monkeypatch.setattr(module, "get_order", customer_at_fault)
    decision = evaluate_service_credit(conn, "ORD-2002")

    assert decision.outcome is PolicyOutcome.NOT_ELIGIBLE
    assert decision.credit_amount is None


def test_unknown_carrier_fault_prevents_a_promise(conn, monkeypatch):
    from app.backend.policies import service_credit as module

    real_get_order = module.get_order

    def unknown_fault(connection, order_id, **kwargs):
        order = real_get_order(connection, order_id, **kwargs)
        if order is None or order.order_id != "ORD-2002":
            return order
        return order.model_copy(update={"carrier_fault": None})

    monkeypatch.setattr(module, "get_order", unknown_fault)
    decision = evaluate_service_credit(conn, "ORD-2002")

    assert decision.outcome is PolicyOutcome.REQUIRES_VERIFICATION
    assert decision.eligible is False
    assert any("carrier fault is unknown" in r.lower() for r in decision.verification_reasons)


def test_unknown_customer_fault_prevents_a_promise(conn, monkeypatch):
    from app.backend.policies import service_credit as module

    real_get_order = module.get_order

    def unknown_customer_fault(connection, order_id, **kwargs):
        order = real_get_order(connection, order_id, **kwargs)
        if order is None or order.order_id != "ORD-2002":
            return order
        return order.model_copy(update={"customer_fault": None})

    monkeypatch.setattr(module, "get_order", unknown_customer_fault)
    decision = evaluate_service_credit(conn, "ORD-2002")

    assert decision.outcome is PolicyOutcome.REQUIRES_VERIFICATION
    assert any("customer fault is unknown" in r.lower() for r in decision.verification_reasons)


def test_missing_pickup_window_prevents_measurement(conn, monkeypatch):
    from app.backend.policies import service_credit as module

    real_get_order = module.get_order

    def no_window(connection, order_id, **kwargs):
        order = real_get_order(connection, order_id, **kwargs)
        if order is None or order.order_id != "ORD-2002":
            return order
        return order.model_copy(update={"pickup_window_end": None})

    monkeypatch.setattr(module, "get_order", no_window)
    decision = evaluate_service_credit(conn, "ORD-2002")

    assert decision.outcome is PolicyOutcome.REQUIRES_VERIFICATION
    assert decision.credit_amount is None


def test_manager_approval_flag_for_a_large_credit(conn, monkeypatch):
    from app.backend.policies import service_credit as module

    real_terms = module.extract_service_credit_terms

    def big_fixed_credit(evidence):
        terms = real_terms(evidence)
        return terms.model_copy(update={"fixed_amount": Decimal("2500")})

    monkeypatch.setattr(module, "extract_service_credit_terms", big_fixed_credit)
    decision = evaluate_service_credit(conn, "ORD-2002")

    assert decision.requires_manager_approval is True
    assert "manager approval" in decision.calculation


def test_incomplete_terms_produce_verification_not_a_guess(conn, monkeypatch):
    from app.backend.models.policy import ServiceCreditTerms
    from app.backend.policies import service_credit as module

    monkeypatch.setattr(
        module, "extract_service_credit_terms", lambda evidence: ServiceCreditTerms()
    )
    decision = evaluate_service_credit(conn, "ORD-2002")

    assert decision.outcome is PolicyOutcome.REQUIRES_VERIFICATION
    assert decision.credit_amount is None


def test_monthly_cap_is_surfaced_when_the_agreement_sets_one(conn, monkeypatch):
    from app.backend.policies import service_credit as module

    real_get_order = module.get_order

    def late_northstar(connection, order_id, **kwargs):
        order = real_get_order(connection, order_id, **kwargs)
        if order is None or order.order_id != "ORD-1001":
            return order
        return order.model_copy(
            update={
                "carrier_fault": True,
                "pickup_actual_at": order.pickup_window_end.replace(hour=23),
            }
        )

    monkeypatch.setattr(module, "get_order", late_northstar)
    decision = evaluate_service_credit(conn, "ORD-1001")

    assert decision.monthly_cap == Decimal("5000")


def test_service_credit_is_deterministic(conn):
    first = evaluate_service_credit(conn, "ORD-2002")
    second = evaluate_service_credit(conn, "ORD-2002")

    assert first.credit_amount == second.credit_amount
    assert first.verification_reasons == second.verification_reasons


def test_service_credit_out_of_scope_order_raises(conn):
    with pytest.raises(PolicyLookupError):
        evaluate_service_credit(conn, "ORD-2002", allowed_account_ids={NORTHSTAR_ACCOUNT})


def test_reference_time_comes_from_the_dataset_snapshot(conn):
    decision = evaluate_service_credit(conn, "ORD-2002")

    assert "dataset snapshot" in decision.inputs["reference_time_source"]
    assert "2026-08-16" in decision.inputs["evaluated_against"]
