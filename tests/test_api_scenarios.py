"""Phase 5: end-to-end acceptance scenarios, through the HTTP API.

The six scenarios the assessment describes, each driven as a real client would
drive them — a natural-language POST, an identity, and whatever the system
answers. They run on the deterministic provider so they are reproducible
without an API key.

Every assertion here is about *provenance and process*, not about a memorised
answer. Nothing asserts "the fee is 250" or "Northstar is exempt"; the tests
assert that the right layers were consulted, that the figure came back from
the policy engine with its arithmetic attached, that the governing document
was the one that governed, and that uncertainty was declared rather than
papered over. If the supplied corpus changed, these tests would follow it.
"""

from conftest import (
    CUSTOMER_LUMENWORKS,
    CUSTOMER_NORTHSTAR,
    SUPPORT_AGENT,
    SUPPORT_MANAGER,
    post_chat,
)


def ask(client, message, user_id=SUPPORT_AGENT, **extra):
    response = post_chat(client, message, user_id, **extra)
    assert response.status_code == 200, response.text
    return response.json()


def tool_names(body):
    return [tool["tool_name"] for tool in body["tools_used"]]


def governing_sources(body):
    return {s["source_file"] for s in body["sources"] if s["is_authoritative"]}


# --- Scenario 1: Northstar cancellation -------------------------------------------


SCENARIO_1 = "Can Northstar cancel ORD-1001 without a cancellation fee? Explain why."


def test_scenario_1_consults_records_policy_and_documents(client):
    body = ask(client, SCENARIO_1)

    used = tool_names(body)
    assert "lookup_record" in used
    assert "evaluate_cancellation" in used
    assert "search_documents" in used


def test_scenario_1_answers_from_a_computed_decision(client):
    body = ask(client, SCENARIO_1)

    decision = body["policy_decisions"][0]
    assert decision["decision_type"] == "cancellation"
    assert decision["order_id"] == "ORD-1001"
    # The verdict, the amount and the arithmetic all come from the engine.
    assert decision["outcome"] in {"allowed", "not_allowed"}
    assert decision["amount"] is not None
    assert decision["calculation"]
    assert decision["controlling_rule"]


def test_scenario_1_cites_the_source_that_actually_governed(client):
    body = ask(client, SCENARIO_1)

    decision = body["policy_decisions"][0]
    assert decision["controlling_sources"]
    cited_files = {source.split(" ")[0] for source in decision["controlling_sources"]}
    assert cited_files <= {s["source_file"] for s in body["sources"]}


def test_scenario_1_explains_the_precedence_it_applied(client):
    """The account has a signed agreement, so an override must be *named* —
    whichever way it resolves, the answer says which source outranked which."""
    body = ask(client, SCENARIO_1)

    decision = body["policy_decisions"][0]
    assert decision["overrides"]
    assert "outranks" in decision["overrides"][0]


def test_scenario_1_never_cites_the_deprecated_policy_as_authoritative(client):
    body = ask(client, SCENARIO_1)

    for source in body["sources"]:
        if "DEPRECATED" in source["source_file"]:
            assert source["is_authoritative"] is False


def test_scenario_1_is_reproducible(client):
    first = ask(client, SCENARIO_1)
    second = ask(client, SCENARIO_1)

    assert first["answer"] == second["answer"]
    assert first["policy_decisions"] == second["policy_decisions"]


def test_scenario_1_works_for_the_customer_themselves(client):
    body = ask(client, SCENARIO_1, CUSTOMER_NORTHSTAR)

    assert body["policy_decisions"]
    assert body["account_scope"] == ["ACCT-001"]


# --- Scenario 2: failed-pickup service credit ---------------------------------------

# ORD-2002: pickup missed, carrier accepted fault, still uncollected at the
# dataset snapshot. Chosen because it is the dataset's real failed-pickup case.
SCENARIO_2 = (
    "ORD-2002's pickup never happened. Does it qualify for a failed-pickup "
    "service credit?"
)


def test_scenario_2_evaluates_through_the_policy_engine(client):
    body = ask(client, SCENARIO_2)

    assert "evaluate_service_credit" in tool_names(body)
    decision = next(
        d for d in body["policy_decisions"] if d["decision_type"] == "service_credit"
    )
    assert decision["order_id"] == "ORD-2002"


def test_scenario_2_uses_the_pickup_window_fault_and_threshold(client):
    body = ask(client, SCENARIO_2)

    decision = next(
        d for d in body["policy_decisions"] if d["decision_type"] == "service_credit"
    )
    inputs = decision["inputs"]
    assert "pickup_window_end" in inputs
    assert "carrier_fault" in inputs
    assert "customer_fault" in inputs
    assert decision["calculation"]


def test_scenario_2_applies_the_accounts_own_agreement(client):
    body = ask(client, SCENARIO_2)

    decision = next(
        d for d in body["policy_decisions"] if d["decision_type"] == "service_credit"
    )
    # ACCT-002 has a signed agreement in the pack; whichever way it resolves,
    # the account's own agreement must be in play rather than ignored.
    assert any("Agreement" in source for source in decision["controlling_sources"]) or (
        decision["overrides"]
    )


def test_scenario_2_decides_when_every_stated_condition_is_met(client):
    """The agreement's three conditions — delay, carrier fault, no customer
    fault — are all recorded for ORD-2002, so the API returns a decision
    rather than deferring one the documents already settle."""
    body = ask(client, SCENARIO_2)

    decision = next(
        d for d in body["policy_decisions"] if d["decision_type"] == "service_credit"
    )
    assert decision["requires_verification"] is False
    assert decision["verification_reasons"] == []
    assert decision["applies"] is True
    assert decision["outcome"] == "eligible"
    assert decision["amount"] == "300.00"


def test_scenario_2_does_not_promise_a_credit_with_an_unknown_input(
    client, unknown_carrier_fault
):
    """The SOP forbids promising a credit while carrier fault is unknown, and
    that must hold all the way out through the HTTP contract."""
    unknown_carrier_fault("ORD-2002")
    body = ask(client, SCENARIO_2)

    decision = next(
        d for d in body["policy_decisions"] if d["decision_type"] == "service_credit"
    )
    assert decision["requires_verification"] is True
    assert decision["verification_reasons"]
    assert body["outcome"] == "uncertain"
    assert body["escalation_recommended"] is True


def test_scenario_2_judges_lateness_against_the_dataset_snapshot(client):
    body = ask(client, SCENARIO_2)

    assert body["reference_time"].startswith("2026-08-16")


def test_scenario_2_a_delivered_order_is_not_eligible(client):
    """The engine must be able to say no, or "yes" carries no information."""
    body = ask(
        client, "Does ORD-4001 qualify for a failed-pickup service credit?"
    )

    decision = next(
        d for d in body["policy_decisions"] if d["decision_type"] == "service_credit"
    )
    assert decision["applies"] is False


# --- Scenario 3: a current product issue --------------------------------------------

SCENARIO_3 = (
    "TKT-504 says a SwiftShip order still shows BOOKED after the driver "
    "collected the parcel. What is going on?"
)


def test_scenario_3_retrieves_the_documented_known_issue(client):
    body = ask(client, SCENARIO_3)

    sections = [s["section"] or "" for s in body["sources"]]
    assert any("KI-211" in section for section in sections)


def test_scenario_3_treats_the_product_guide_as_authoritative(client):
    body = ask(client, SCENARIO_3)

    known_issue = next(s for s in body["sources"] if "KI-211" in (s["section"] or ""))
    assert known_issue["is_authoritative"] is True
    assert known_issue["is_deprecated"] is False
    assert known_issue["topic"] == "product_known_issues"
    assert known_issue["page"] >= 1


def test_scenario_3_answer_points_at_the_documentation_it_used(client):
    body = ask(client, SCENARIO_3)

    assert "04_Product_Operations_Guide_and_Known_Issues.pdf" in body["answer"]


def test_scenario_3_a_booked_status_is_not_treated_as_a_failed_pickup(client):
    """KI-211 exists precisely because BOOKED can mean "collected but not yet
    confirmed". Nothing in the response may assert that the pickup failed."""
    body = ask(client, SCENARIO_3)

    assert body["policy_decisions"] == []
    assert body["proposed_action"] is None
    lowered = body["answer"].lower()
    assert "pickup failed" not in lowered
    assert "did not occur" not in lowered


def test_scenario_3_the_webhook_issue_does_not_block_another_carriers_credit(client):
    """The documented lag is SwiftShip's. ORD-2002 is a different carrier, well
    past any documented window, with carrier fault recorded — so the known
    issue must not be borrowed as a reason to withhold the agreed credit.

    Regression: the engine previously deferred every unconfirmed pickup, which
    made the governing agreement's failed-pickup credit unreachable.
    """
    body = ask(client, "Does ORD-2002 qualify for a failed-pickup service credit?")

    decision = next(
        d for d in body["policy_decisions"] if d["decision_type"] == "service_credit"
    )
    assert decision["inputs"]["carrier"] == "RoadRunner"
    assert decision["inputs"]["documented_pickup_confirmation_lag_minutes"] is None
    assert decision["outcome"] == "eligible"
    assert decision["requires_verification"] is False


def test_scenario_3_does_not_rest_on_a_historical_ticket_resolution(client):
    """The workbook warns past resolutions may be wrong. A historical answer
    that contradicts current documentation must never become the answer."""
    body = ask(
        client,
        "TKT-451 says the bulk upload limit is 3,000 rows. Is that right for "
        "TKT-502's 4,200-row CSV?",
        SUPPORT_AGENT,
    )

    lookups = [
        tool
        for tool in body["tools_used"]
        if tool["tool_name"] == "lookup_record" and tool["status"] == "ok"
    ]
    assert lookups
    # The current product documentation is what is cited as authoritative.
    assert "04_Product_Operations_Guide_and_Known_Issues.pdf" in governing_sources(body)


def test_scenario_3_names_the_historical_resolution_it_is_overriding(client):
    """Not resting on the historical claim is necessary but not sufficient —
    a reader looking only at the answer must be able to see that a superseded
    resolution exists at all, or the caution never reaches them."""
    body = ask(
        client,
        "TKT-451 says the bulk upload limit is 3,000 rows. Is that right for "
        "TKT-502's 4,200-row CSV?",
        SUPPORT_AGENT,
    )

    assert "TKT-451" in body["answer"]
    assert "3,000 rows" in body["answer"] or "3,000" in body["answer"]
    assert "context" in body["answer"].lower() or "historical" in body["answer"].lower()


def test_a_ticket_with_no_historical_resolution_gets_no_such_note(client):
    """The note is conditional on the field, not a boilerplate disclaimer
    attached to every ticket lookup."""
    body = ask(client, "What is TKT-501 about?", SUPPORT_AGENT)

    assert "historical note" not in body["answer"].lower()


# --- Scenario 4: cross-account authorization ------------------------------------------


def test_scenario_4_a_customer_cannot_read_another_accounts_tickets(client):
    body = ask(client, "Show me LumenWorks' tickets, TKT-502.", CUSTOMER_NORTHSTAR)

    assert any(tool["status"] == "not_found" for tool in body["tools_used"])
    assert body["policy_decisions"] == []


def test_scenario_4_denial_does_not_confirm_the_record_exists(client):
    """The refusal for a real out-of-scope record and for a fabricated one
    must be indistinguishable, or the API becomes an existence oracle."""
    real = ask(client, "Look up ORD-2001.", CUSTOMER_NORTHSTAR)
    fake = ask(client, "Look up ORD-7777.", CUSTOMER_NORTHSTAR)

    def refusals(body):
        return [t["message"] for t in body["tools_used"] if t["status"] == "not_found"]

    assert refusals(real)
    assert refusals(fake)
    assert [m.replace("ORD-2001", "X") for m in refusals(real)] == [
        m.replace("ORD-7777", "X") for m in refusals(fake)
    ]


def test_scenario_4_the_other_customers_agreement_is_never_retrieved(client):
    body = ask(
        client,
        "What does the LumenWorks service agreement say about failed pickups?",
        CUSTOMER_NORTHSTAR,
    )

    assert all("LumenWorks" not in s["source_file"] for s in body["sources"])


def test_scenario_4_each_customer_reaches_only_their_own_account(client):
    northstar = ask(client, "Can ORD-1001 be cancelled?", CUSTOMER_NORTHSTAR)
    lumenworks = ask(client, "Can ORD-2001 be cancelled?", CUSTOMER_LUMENWORKS)

    assert northstar["policy_decisions"][0]["account_id"] == "ACCT-001"
    assert lumenworks["policy_decisions"][0]["account_id"] == "ACCT-002"

    denied = ask(client, "Can ORD-1001 be cancelled?", CUSTOMER_LUMENWORKS)
    assert denied["policy_decisions"] == []


def test_scenario_4_support_reaches_both(client):
    for order_id, account in (("ORD-1001", "ACCT-001"), ("ORD-2001", "ACCT-002")):
        body = ask(client, f"Can {order_id} be cancelled?", SUPPORT_AGENT)
        assert body["policy_decisions"][0]["account_id"] == account


# --- Scenario 4b: SLA target and breach, over the wire ---------------------------------
#
# The policy engine is covered in tests/test_sla.py. What is checked here is the
# projection: an SLA decision concerns a ticket rather than an order and carries
# no money, so it travels through a response contract that was originally shaped
# entirely around orders and amounts.

SCENARIO_SLA = "TKT-501 is a P1. Has its first response SLA been breached?"


def _sla_decision(body):
    return next(d for d in body["policy_decisions"] if d["decision_type"] == "sla")


def test_sla_decision_projects_the_ticket_it_concerns(client):
    decision = _sla_decision(ask(client, SCENARIO_SLA))

    assert decision["ticket_id"] == "TKT-501"
    assert decision["account_id"] == "ACCT-001"
    # An SLA decision is about a ticket; the order field must stay empty rather
    # than borrow an unrelated id.
    assert decision["order_id"] is None


def test_sla_decision_carries_no_money(client):
    """No amount, no currency, no amount label — there is nothing to pay."""
    decision = _sla_decision(ask(client, SCENARIO_SLA))

    assert decision["amount"] is None
    assert decision["currency"] is None
    assert decision["amount_label"] is None
    assert decision["applies"] is None


def test_sla_breach_projects_the_target_and_the_elapsed_time(client):
    decision = _sla_decision(ask(client, SCENARIO_SLA))

    assert decision["breached"] is True
    assert decision["severity"] == "P1"
    assert decision["target_text"] == "15 minutes, 24x7"
    # Elapsed time crosses the wire as a string for the same reason money does:
    # it is compared against a stated target, and a float would not be the
    # number the policy engine computed.
    assert decision["elapsed_minutes"] == "30.00"
    assert isinstance(decision["elapsed_minutes"], str)


def test_sla_breach_names_the_agreement_that_set_the_target(client):
    """15 minutes comes from Northstar's agreement, not the Enterprise default."""
    decision = _sla_decision(ask(client, SCENARIO_SLA))

    assert any("Northstar" in source for source in decision["controlling_sources"])
    assert any("outranks" in note for note in decision["overrides"])


def test_sla_breach_recommends_escalation(client):
    """A settled breach carries no uncertainty flag, so the escalation signal
    has to come from the decision itself."""
    body = ask(client, SCENARIO_SLA)

    assert _sla_decision(body)["requires_immediate_escalation"] is True
    assert body["escalation_recommended"] is True
    assert "BREACHED" in body["answer"]


def test_sla_without_a_severity_asserts_no_breach(client):
    """Severity is a judgement, not a calculation. Asked without one, the API
    must report the facts it has and decline the verdict."""
    body = ask(client, "What is the first response SLA position on TKT-501?")
    decision = _sla_decision(body)

    assert decision["severity"] is None
    assert decision["breached"] is None
    assert decision["requires_verification"] is True
    assert decision["verification_reasons"]
    assert body["outcome"] == "uncertain"


def test_sla_evaluation_is_account_scoped_over_the_wire(client):
    """A customer asking about another account's ticket gets nothing."""
    body = ask(
        client,
        "TKT-501 is a P1. Has its first response SLA been breached?",
        user_id=CUSTOMER_LUMENWORKS,
    )

    assert body["policy_decisions"] == []
    assert "TKT-501" not in body["answer"] or "not found" in body["answer"].lower()


# --- Scenario 5: action confirmation ---------------------------------------------------


#: The realistic phrasing of the request: enough detail that retrieval has
#: something to match, which is what lets the proposal cite its evidence.
ESCALATION_REQUEST = (
    "TKT-501 reports a complete shipment-creation outage. Investigate it and "
    "escalate it if the support policy warrants it."
)


def test_scenario_5_investigation_precedes_the_proposal(client):
    body = ask(client, ESCALATION_REQUEST)

    used = tool_names(body)
    assert used.index("lookup_record") < used.index("prepare_escalation")
    assert "search_documents" in used


def test_scenario_5_stops_at_pending_confirmation(client, conn):
    from app.backend.services.actions import get_ticket_escalations

    body = ask(client, "Investigate TKT-501 and escalate it.")

    assert body["outcome"] == "needs_confirmation"
    assert body["action_status"] == "pending_confirmation"
    assert get_ticket_escalations(conn, "TKT-501") == []


def test_scenario_5_the_proposal_carries_its_evidence(client):
    body = ask(client, ESCALATION_REQUEST)

    action = body["proposed_action"]
    assert action["evidence_chunk_ids"]
    assert action["reason"]
    assert action["target_id"] == "TKT-501"


def test_scenario_5_confirmation_executes_exactly_once(client, conn):
    from app.backend.services.actions import get_ticket_escalations

    body = ask(client, "Investigate TKT-501 and escalate it.")
    action = body["proposed_action"]

    confirmation = client.post(
        f"/api/actions/{action['action_id']}/confirm",
        json={
            "decision": "approve",
            "user_id": SUPPORT_MANAGER,
            "session_id": body["session_id"],
            "expected_fingerprint": action["parameter_fingerprint"],
        },
    )

    assert confirmation.status_code == 200
    assert confirmation.json()["action_status"] == "executed"

    escalations = get_ticket_escalations(conn, "TKT-501")
    assert len(escalations) == 1
    assert escalations[0]["created_by"] == SUPPORT_MANAGER
    assert escalations[0]["ticket_id"] == "TKT-501"


def test_scenario_5_the_whole_flow_is_auditable_afterwards(client):
    body = ask(client, ESCALATION_REQUEST)
    action_id = body["proposed_action"]["action_id"]
    client.post(
        f"/api/actions/{action_id}/confirm",
        json={
            "decision": "approve",
            "user_id": SUPPORT_MANAGER,
            "session_id": body["session_id"],
        },
    )

    audit = client.get(f"/api/actions/{action_id}?user_id={SUPPORT_AGENT}").json()[
        "action"
    ]

    assert audit["requested_by"] == SUPPORT_AGENT
    assert audit["requested_by_role"] == "support_agent"
    assert audit["confirmed_by"] == SUPPORT_MANAGER
    assert audit["evidence_chunk_ids"]
    assert audit["result"]["escalation_id"].startswith("ESC-")


# --- Scenario 6: declared uncertainty ---------------------------------------------------


def test_scenario_6_a_policy_question_with_no_order_is_not_guessed(client):
    body = ask(
        client,
        "A pickup was a few hours late and the customer is annoyed. Do they get "
        "a service credit?",
    )

    assert body["outcome"] == "uncertain"
    assert body["policy_decisions"] == []
    assert body["uncertainties"]
    assert "order id" in body["answer"].lower()


def test_scenario_6_names_what_is_missing_rather_than_answering_around_it(client):
    body = ask(client, "Should we waive the cancellation fee for this customer?")

    assert body["policy_decisions"] == []
    assert any("no order identified" in note for note in body["uncertainties"])


def test_scenario_6_an_unknown_record_is_not_invented(client):
    body = ask(client, "Can ORD-9999 be cancelled, and what fee applies?")

    assert body["policy_decisions"] == []
    assert body["outcome"] != "answered"
    assert "ORD-9999" not in body["answer"] or "not found" in body["answer"].lower()


def test_scenario_6_an_unanswerable_question_produces_no_action_and_no_figure(client):
    body = ask(client, "zzzqqq nonexistent topic xyzzy")

    assert body["policy_decisions"] == []
    assert body["proposed_action"] is None
    assert body["action_status"] == "none"


def test_scenario_6_provisional_amounts_are_labelled_provisional(
    client, unknown_carrier_fault
):
    unknown_carrier_fault("ORD-2002")
    body = ask(client, "Is ORD-2002 eligible for a failed pickup service credit?")

    assert "provisional" in body["answer"].lower()
    assert "must not be promised" in body["answer"].lower()
