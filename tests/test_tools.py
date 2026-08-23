"""Phase 4: tool contracts, dispatch, and the authorization boundary.

The security-relevant claim under test is that authorization cannot be
influenced by anything a model emits: scope comes from AgentContext, tool
arguments cannot carry it, and the reachable tool surface excludes execution
entirely.
"""

import pytest

from app.backend.models.agent import AgentContext, Role, ToolStatus
from app.backend.tools.base import RESERVED_ARGUMENT_NAMES, ToolRegistry, ToolSpec
from app.backend.tools.registry import build_default_registry
from conftest import LUMENWORKS_ACCOUNT, NORTHSTAR_ACCOUNT


@pytest.fixture
def registry():
    return build_default_registry()


# --- registry surface -----------------------------------------------------------


def test_registry_exposes_the_required_tool_categories(registry):
    names = registry.names()

    assert "search_documents" in names  # document retrieval
    assert "lookup_record" in names  # structured lookup
    assert "evaluate_cancellation" in names  # deterministic policy
    assert "evaluate_service_credit" in names
    assert "prepare_escalation" in names  # state-changing preparation


def test_no_tool_can_execute_a_state_changing_action(registry):
    """Execution is deliberately not in the model's reachable surface."""
    for name in registry.names():
        assert not name.startswith("execute_")
        assert "confirm" not in name
    assert all("execute" not in name for name in registry.names())


def test_tool_schemas_are_well_formed(registry):
    for schema in registry.schemas():
        assert schema["name"]
        assert schema["description"]
        assert schema["parameters"]["type"] == "object"


def test_schemas_are_stable_in_order(registry):
    assert registry.schemas() == build_default_registry().schemas()


def test_duplicate_registration_is_rejected():
    custom = ToolRegistry()
    spec = ToolSpec("t", "d", {"type": "object", "properties": {}}, lambda c, x, a: None)
    custom.register(spec)

    with pytest.raises(ValueError, match="already registered"):
        custom.register(spec)


def test_unknown_tool_returns_invalid_input(conn, registry, agent_context):
    result = registry.execute(conn, agent_context, "no_such_tool", {})

    assert result.status is ToolStatus.INVALID_INPUT
    assert "unknown tool" in result.message


def test_tool_exception_becomes_an_error_not_an_answer(conn, agent_context):
    def explode(connection, context, arguments):
        raise RuntimeError("boom")

    custom = ToolRegistry()
    custom.register(ToolSpec("boom", "d", {"type": "object", "properties": {}}, explode))

    result = custom.execute(conn, agent_context, "boom", {})

    assert result.status is ToolStatus.ERROR
    assert "boom" in result.message


# --- authorization cannot be supplied by the caller of a tool ----------------------


@pytest.mark.parametrize("reserved", sorted(RESERVED_ARGUMENT_NAMES))
def test_reserved_arguments_are_rejected(conn, registry, northstar_context, reserved):
    result = registry.execute(
        conn,
        northstar_context,
        "lookup_record",
        {"entity": "order", "order_id": "ORD-2001", reserved: ["ACCT-002"]},
    )

    assert result.status is ToolStatus.FORBIDDEN
    assert reserved in result.message


def test_scope_widening_attempt_does_not_return_data(conn, registry, northstar_context):
    result = registry.execute(
        conn,
        northstar_context,
        "lookup_record",
        {"entity": "order", "order_id": "ORD-2001", "allowed_account_ids": ["ACCT-002"]},
    )

    assert result.status is ToolStatus.FORBIDDEN
    assert result.data == {}


def test_readonly_role_cannot_prepare_actions(conn, registry, readonly_context):
    result = registry.execute(
        conn, readonly_context, "prepare_escalation", {"ticket_id": "TKT-501", "reason": "x"}
    )

    assert result.status is ToolStatus.FORBIDDEN
    assert "read_only" in result.message


def test_readonly_role_may_still_read(conn, registry, readonly_context):
    result = registry.execute(
        conn, readonly_context, "lookup_record", {"entity": "order", "order_id": "ORD-1001"}
    )

    assert result.status is ToolStatus.OK


# --- Tool B: structured lookup ------------------------------------------------------


def test_lookup_order_returns_the_record(conn, registry, agent_context):
    result = registry.execute(
        conn, agent_context, "lookup_record", {"entity": "order", "order_id": "ORD-1001"}
    )

    assert result.status is ToolStatus.OK
    assert result.data["record"]["order_id"] == "ORD-1001"
    assert result.data["record"]["account_id"] == NORTHSTAR_ACCOUNT


def test_lookup_account_and_ticket(conn, registry, agent_context):
    account = registry.execute(
        conn, agent_context, "lookup_record", {"entity": "account", "account_id": NORTHSTAR_ACCOUNT}
    )
    ticket = registry.execute(
        conn, agent_context, "lookup_record", {"entity": "ticket", "ticket_id": "TKT-501"}
    )

    assert account.data["record"]["account_name"] == "Northstar Logistics"
    assert ticket.data["record"]["ticket_id"] == "TKT-501"


def test_lookup_account_collections(conn, registry, agent_context):
    orders = registry.execute(
        conn, agent_context, "lookup_record",
        {"entity": "account_orders", "account_id": NORTHSTAR_ACCOUNT},
    )
    tickets = registry.execute(
        conn, agent_context, "lookup_record",
        {"entity": "account_tickets", "account_id": NORTHSTAR_ACCOUNT},
    )

    assert orders.data["count"] == 2  # ORD-1001, ORD-1002
    # TKT-501 and TKT-504 are open; TKT-450 is closed but still this account's.
    assert tickets.data["count"] == 3
    assert all(r["account_id"] == NORTHSTAR_ACCOUNT for r in tickets.data["records"])


def test_lookup_dataset_metadata(conn, registry, agent_context):
    result = registry.execute(
        conn, agent_context, "lookup_record", {"entity": "dataset_metadata"}
    )

    assert result.status is ToolStatus.OK
    assert "Asia/Kolkata" in result.data["record"]["dataset_snapshot"]


def test_historical_resolution_is_flagged_as_non_authoritative(conn, registry, agent_context):
    """The workbook warns these may be wrong; the flag stops a past
    resolution reading like current policy."""
    result = registry.execute(
        conn, agent_context, "lookup_record", {"entity": "ticket", "ticket_id": "TKT-450"}
    )

    assert result.data["record"]["historical_resolution"]
    assert "not" in result.data["record"]["historical_resolution_warning"].lower()


def test_lookup_rejects_unknown_entity(conn, registry, agent_context):
    result = registry.execute(conn, agent_context, "lookup_record", {"entity": "salaries"})

    assert result.status is ToolStatus.INVALID_INPUT


def test_lookup_requires_an_id(conn, registry, agent_context):
    result = registry.execute(conn, agent_context, "lookup_record", {"entity": "order"})

    assert result.status is ToolStatus.INVALID_INPUT
    assert "order_id" in result.message


def test_unknown_record_is_not_found(conn, registry, agent_context):
    result = registry.execute(
        conn, agent_context, "lookup_record", {"entity": "order", "order_id": "ORD-9999"}
    )

    assert result.status is ToolStatus.NOT_FOUND


def test_out_of_scope_record_matches_the_missing_record_message(conn, registry, northstar_context):
    """Out-of-scope and non-existent must be indistinguishable, or the tool
    becomes an existence oracle for other customers' data."""
    out_of_scope = registry.execute(
        conn, northstar_context, "lookup_record", {"entity": "order", "order_id": "ORD-2001"}
    )
    missing = registry.execute(
        conn, northstar_context, "lookup_record", {"entity": "order", "order_id": "ORD-9999"}
    )

    assert out_of_scope.status is missing.status is ToolStatus.NOT_FOUND
    assert out_of_scope.message.replace("ORD-2001", "X") == missing.message.replace(
        "ORD-9999", "X"
    )


def test_provenance_tool_requires_a_readable_record(conn, registry, northstar_context):
    allowed = registry.execute(
        conn, northstar_context, "lookup_record_provenance",
        {"target_table": "orders", "target_id": "ORD-1001"},
    )
    denied = registry.execute(
        conn, northstar_context, "lookup_record_provenance",
        {"target_table": "orders", "target_id": "ORD-2001"},
    )

    assert allowed.status is ToolStatus.OK
    assert allowed.data["source_file"].endswith(".xlsx")
    assert denied.status is ToolStatus.NOT_FOUND


# --- Tool A: document retrieval --------------------------------------------------------


def test_search_returns_evidence_with_provenance(conn, registry, agent_context):
    result = registry.execute(
        conn, agent_context, "search_documents", {"query": "cancellation fee after 30 minutes"}
    )

    assert result.status is ToolStatus.OK
    assert result.evidence
    first = result.data["governing"][0] if result.data["governing"] else result.data["contextual"][0]
    assert first["source_file"].endswith(".pdf")
    assert first["page"] >= 1
    assert "citation" in first


def test_search_separates_governing_from_contextual(conn, registry, agent_context):
    result = registry.execute(
        conn,
        agent_context,
        "search_documents",
        {"query": "cancellation fee for a booked shipment", "account_id": NORTHSTAR_ACCOUNT},
    )

    governing_files = {g["source_file"] for g in result.data["governing"]}
    assert any("Northstar" in f for f in governing_files)
    assert result.data["overrides"]


def test_search_never_returns_another_customers_agreement(conn, registry, northstar_context):
    result = registry.execute(
        conn,
        northstar_context,
        "search_documents",
        {"query": "LumenWorks fixed INR 300 credit", "limit": 25},
    )

    assert all("LumenWorks" not in e.source_file for e in result.evidence)


def test_search_account_id_argument_cannot_exceed_context_scope(conn, registry, northstar_context):
    """Even naming another account explicitly, the context still binds."""
    result = registry.execute(
        conn,
        northstar_context,
        "search_documents",
        {"query": "agreement credit terms", "account_id": LUMENWORKS_ACCOUNT, "limit": 25},
    )

    assert all(e.account_id in (None, NORTHSTAR_ACCOUNT) for e in result.evidence)


def test_search_with_no_match_reports_no_evidence(conn, registry, agent_context):
    result = registry.execute(
        conn, agent_context, "search_documents", {"query": "zzzqqqxyzzy"}
    )

    assert result.status is ToolStatus.NO_EVIDENCE
    assert result.evidence == []


def test_search_validates_arguments(conn, registry, agent_context):
    assert (
        registry.execute(conn, agent_context, "search_documents", {}).status
        is ToolStatus.INVALID_INPUT
    )
    assert (
        registry.execute(
            conn, agent_context, "search_documents", {"query": "x", "limit": 500}
        ).status
        is ToolStatus.INVALID_INPUT
    )


def test_get_document_evidence_requires_one_selector(conn, registry, agent_context):
    result = registry.execute(conn, agent_context, "get_document_evidence", {})

    assert result.status is ToolStatus.INVALID_INPUT


def test_get_document_evidence_respects_scope(conn, registry, northstar_context):
    result = registry.execute(
        conn,
        northstar_context,
        "get_document_evidence",
        {"document_id": "06_lumenworks_service_agreement"},
    )

    assert result.status is ToolStatus.NOT_FOUND


# --- Tool C: policy ----------------------------------------------------------------------


def test_policy_tool_returns_a_decision_not_prose(conn, registry, agent_context):
    result = registry.execute(
        conn, agent_context, "evaluate_cancellation", {"order_id": "ORD-1001"}
    )

    assert result.status is ToolStatus.OK
    assert len(result.decisions) == 1
    decision = result.decisions[0]
    assert decision.fee_amount is not None
    assert decision.controlling_sources


def test_policy_tool_reports_uncertainty_as_a_distinct_status(
    conn, registry, agent_context, unknown_carrier_fault
):
    """A deferred decision must not reach the planner as an ordinary success —
    the orchestrator escalates on UNCERTAIN, so the distinction is load-bearing."""
    unknown_carrier_fault("ORD-2002")
    result = registry.execute(
        conn, agent_context, "evaluate_service_credit", {"order_id": "ORD-2002"}
    )

    assert result.status is ToolStatus.UNCERTAIN
    assert result.message


def test_policy_tool_reports_a_settled_decision_as_ok(conn, registry, agent_context):
    result = registry.execute(
        conn, agent_context, "evaluate_service_credit", {"order_id": "ORD-2002"}
    )

    assert result.status is ToolStatus.OK
    assert result.decisions[0].eligible is True


def test_policy_tool_enforces_scope(conn, registry, northstar_context):
    result = registry.execute(
        conn, northstar_context, "evaluate_cancellation", {"order_id": "ORD-2001"}
    )

    assert result.status is ToolStatus.NOT_FOUND
    assert result.decisions == []


def test_policy_tool_validates_input(conn, registry, agent_context):
    result = registry.execute(conn, agent_context, "evaluate_cancellation", {})

    assert result.status is ToolStatus.INVALID_INPUT
