"""Phase 5: the real (model-backed) planning provider.

Every test here drives `OpenAIPlanningProvider` against a scripted fake client.
No API key is needed and no network call is made — which is the point: the
provider seam Phase 4 defined is what makes a live model swappable *and*
testable, and a real-provider integration that could only be exercised with a
paid key would leave its failure modes unverified.

What is under test is the adapter's behaviour: that it drives a genuine
multi-step tool loop, that it feeds failures back honestly, that it recovers
from malformed tool calls, that it cannot reach past the tool surface, and
that provider failures surface as errors rather than as prose.
"""

from types import SimpleNamespace

import pytest

from app.backend.agent.openai_provider import (
    OpenAIPlanningProvider,
    serialise_tool_result,
)
from app.backend.agent.orchestrator import AgentOrchestrator
from app.backend.core.errors import ProviderError, ProviderTimeoutError
from app.backend.models.agent import AgentRequest, ToolResult, ToolStatus
from app.backend.tools.registry import build_default_registry
from conftest import NORTHSTAR_ACCOUNT


# --- a scripted stand-in for the OpenAI SDK ------------------------------------


def tool_call(call_id: str, name: str, arguments: str):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def reply(*, content=None, calls=()):
    message = SimpleNamespace(content=content, tool_calls=list(calls) or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeCompletions:
    """Returns scripted replies in order, recording what it was sent."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._replies:
            raise AssertionError("the model was called more times than scripted")
        nxt = self._replies.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class FakeClient:
    def __init__(self, replies):
        self.completions = FakeCompletions(replies)
        self.chat = SimpleNamespace(completions=self.completions)


def make_provider(replies, **kwargs):
    client = FakeClient(replies)
    provider = OpenAIPlanningProvider(client=client, model="gpt-4o-test", **kwargs)
    return provider, client


def run(conn, context, message, replies, **kwargs):
    provider, client = make_provider(replies, **kwargs)
    orchestrator = AgentOrchestrator(conn, provider=provider)
    response = orchestrator.handle(AgentRequest(message=message, context=context))
    return response, client


# --- the tool loop ----------------------------------------------------------------


def test_the_model_can_chain_several_tool_calls(conn, agent_context):
    response, client = run(
        conn,
        agent_context,
        "Can Northstar cancel ORD-1001 without a fee?",
        [
            reply(calls=[tool_call("c1", "lookup_record", '{"entity": "order", "order_id": "ORD-1001"}')]),
            reply(calls=[tool_call("c2", "evaluate_cancellation", '{"order_id": "ORD-1001"}')]),
            reply(calls=[tool_call("c3", "search_documents", '{"query": "cancellation fee", "account_id": "ACCT-001"}')]),
            reply(content="ORD-1001 can be cancelled with no fee under the agreement."),
        ],
    )

    assert response.tools_used == [
        "lookup_record",
        "evaluate_cancellation",
        "search_documents",
    ]
    assert response.answer.startswith("ORD-1001 can be cancelled")


def test_the_models_prose_becomes_the_answer(conn, agent_context):
    response, _ = run(
        conn,
        agent_context,
        "What is the cancellation policy?",
        [
            reply(calls=[tool_call("c1", "search_documents", '{"query": "cancellation"}')]),
            reply(content="Cancellations within the free window carry no fee."),
        ],
    )

    assert response.answer == "Cancellations within the free window carry no fee."


def test_several_tool_calls_in_one_turn_are_all_executed(conn, agent_context):
    response, client = run(
        conn,
        agent_context,
        "Look at ORD-1001 and TKT-501.",
        [
            reply(
                calls=[
                    tool_call("c1", "lookup_record", '{"entity": "order", "order_id": "ORD-1001"}'),
                    tool_call("c2", "lookup_record", '{"entity": "ticket", "ticket_id": "TKT-501"}'),
                ]
            ),
            reply(content="Both records exist."),
        ],
    )

    assert len(response.tool_invocations) == 2

    # Each result must come back paired to the id that requested it, or the
    # provider would build an invalid transcript.
    final_messages = client.completions.calls[-1]["messages"]
    tool_ids = [m["tool_call_id"] for m in final_messages if m["role"] == "tool"]
    assert tool_ids == ["c1", "c2"]


def test_the_model_receives_the_tool_results_it_asked_for(conn, agent_context):
    _, client = run(
        conn,
        agent_context,
        "Can ORD-1001 be cancelled?",
        [
            reply(calls=[tool_call("c1", "evaluate_cancellation", '{"order_id": "ORD-1001"}')]),
            reply(content="No fee applies."),
        ],
    )

    tool_messages = [
        m for m in client.completions.calls[-1]["messages"] if m["role"] == "tool"
    ]
    assert len(tool_messages) == 1
    assert "controlling_rule" in tool_messages[0]["content"]


def test_the_tool_schemas_are_handed_to_the_model_verbatim(conn, agent_context):
    _, client = run(
        conn,
        agent_context,
        "hello",
        [reply(content="Hello.")],
    )

    sent = {t["function"]["name"] for t in client.completions.calls[0]["tools"]}
    assert sent == set(build_default_registry().names())


def test_the_system_prompt_states_the_boundaries_without_the_answers(
    conn, agent_context
):
    _, client = run(conn, agent_context, "hello", [reply(content="Hello.")])

    system = " ".join(
        m["content"] for m in client.completions.calls[0]["messages"] if m["role"] == "system"
    )
    assert "prepare" in system.lower()
    assert "confirm" in system.lower()
    # The prompt must not encode the corpus's actual answers.
    for leak in ("Northstar", "LumenWorks", "KI-211", "INR 250", "INR 300"):
        assert leak not in system


def test_the_prompt_carries_the_dataset_reference_time_not_today(conn, agent_context):
    from app.backend.policies.base import load_evaluation_context

    reference = load_evaluation_context(conn).reference_time
    _, client = run(
        conn,
        agent_context,
        "hello",
        [reply(content="Hello.")],
        reference_time=reference,
    )

    system = " ".join(
        m["content"] for m in client.completions.calls[0]["messages"] if m["role"] == "system"
    )
    assert "2026-08-16" in system
    assert "dataset snapshot" in system


# --- malformed and unknown tool calls -------------------------------------------------


def test_malformed_tool_arguments_are_repaired_not_crashed(conn, agent_context):
    response, client = run(
        conn,
        agent_context,
        "Can ORD-1001 be cancelled?",
        [
            reply(calls=[tool_call("c1", "evaluate_cancellation", "{not json at all")]),
            reply(calls=[tool_call("c2", "evaluate_cancellation", '{"order_id": "ORD-1001"}')]),
            reply(content="No fee applies."),
        ],
    )

    assert response.answer == "No fee applies."
    assert len(response.tool_invocations) == 1

    repair = [
        m
        for m in client.completions.calls[1]["messages"]
        if m["role"] == "tool" and "not valid JSON" in m["content"]
    ]
    assert repair


def test_persistently_malformed_tool_calls_raise_rather_than_answer(conn, agent_context):
    provider, _ = make_provider(
        [reply(calls=[tool_call(f"c{i}", "lookup_record", "{{{")]) for i in range(3)]
    )
    orchestrator = AgentOrchestrator(conn, provider=provider)

    with pytest.raises(ProviderError, match="could not be parsed"):
        orchestrator.handle(AgentRequest(message="go", context=agent_context))


def test_arguments_that_are_not_an_object_are_rejected(conn, agent_context):
    response, _ = run(
        conn,
        agent_context,
        "Can ORD-1001 be cancelled?",
        [
            reply(calls=[tool_call("c1", "evaluate_cancellation", '"ORD-1001"')]),
            reply(calls=[tool_call("c2", "evaluate_cancellation", '{"order_id": "ORD-1001"}')]),
            reply(content="No fee applies."),
        ],
    )

    assert response.answer == "No fee applies."


def test_an_unknown_tool_is_answered_with_the_real_tool_list(conn, agent_context):
    response, client = run(
        conn,
        agent_context,
        "Run some SQL.",
        [
            reply(calls=[tool_call("c1", "run_sql", '{"sql": "SELECT * FROM accounts"}')]),
            reply(content="I cannot query the database directly."),
        ],
    )

    assert response.tool_invocations[0].status is ToolStatus.INVALID_INPUT
    feedback = [
        m for m in client.completions.calls[1]["messages"] if m["role"] == "tool"
    ][0]["content"]
    assert "unknown tool" in feedback
    assert "lookup_record" in feedback


def test_an_empty_reply_is_challenged_then_accepted(conn, agent_context):
    response, _ = run(
        conn,
        agent_context,
        "hello",
        [reply(content=None), reply(content="Hello.")],
    )

    assert response.answer == "Hello."


def test_a_model_that_never_answers_raises(conn, agent_context):
    provider, _ = make_provider([reply(content=None) for _ in range(3)])
    orchestrator = AgentOrchestrator(conn, provider=provider)

    with pytest.raises(ProviderError, match="neither an answer nor a tool call"):
        orchestrator.handle(AgentRequest(message="go", context=agent_context))


# --- provider failures ---------------------------------------------------------------


def test_an_api_failure_surfaces_as_a_provider_error(conn, agent_context):
    class APIConnectionError(Exception):
        pass

    provider, _ = make_provider([APIConnectionError("no route to host")])
    orchestrator = AgentOrchestrator(conn, provider=provider)

    with pytest.raises(ProviderError) as excinfo:
        orchestrator.handle(AgentRequest(message="go", context=agent_context))

    assert "no route to host" not in str(excinfo.value)


def test_a_timeout_is_reported_as_a_timeout(conn, agent_context):
    class APITimeoutError(Exception):
        pass

    provider, _ = make_provider([APITimeoutError("took too long")])
    orchestrator = AgentOrchestrator(conn, provider=provider)

    with pytest.raises(ProviderTimeoutError):
        orchestrator.handle(AgentRequest(message="go", context=agent_context))


def test_a_provider_failure_never_becomes_an_answer(conn, agent_context):
    class RateLimitError(Exception):
        pass

    provider, _ = make_provider([RateLimitError("slow down")])
    orchestrator = AgentOrchestrator(conn, provider=provider)

    with pytest.raises(ProviderError):
        orchestrator.handle(AgentRequest(message="go", context=agent_context))


def test_a_malformed_sdk_response_raises(conn, agent_context):
    provider, _ = make_provider([SimpleNamespace(choices=[])])
    orchestrator = AgentOrchestrator(conn, provider=provider)

    with pytest.raises(ProviderError, match="unexpected shape"):
        orchestrator.handle(AgentRequest(message="go", context=agent_context))


# --- the boundaries the model cannot cross ---------------------------------------------


def test_the_model_cannot_widen_its_account_scope(conn, northstar_context):
    response, _ = run(
        conn,
        northstar_context,
        "Get ORD-2001.",
        [
            reply(
                calls=[
                    tool_call(
                        "c1",
                        "lookup_record",
                        '{"entity": "order", "order_id": "ORD-2001", '
                        '"allowed_account_ids": ["ACCT-001", "ACCT-002"]}',
                    )
                ]
            ),
            reply(content="I could not retrieve that order."),
        ],
    )

    assert response.tool_invocations[0].status is ToolStatus.FORBIDDEN
    assert response.decisions == []


def test_the_model_cannot_reach_another_account_even_without_a_scope_argument(
    conn, northstar_context
):
    response, _ = run(
        conn,
        northstar_context,
        "Get ORD-2001.",
        [
            reply(calls=[tool_call("c1", "lookup_record", '{"entity": "order", "order_id": "ORD-2001"}')]),
            reply(content="That order is not available."),
        ],
    )

    assert response.tool_invocations[0].status is ToolStatus.NOT_FOUND


def test_the_model_has_no_execution_tool_to_call(conn, agent_context):
    _, client = run(conn, agent_context, "hello", [reply(content="Hello.")])

    offered = {t["function"]["name"] for t in client.completions.calls[0]["tools"]}
    assert "confirm_action" not in offered
    assert "execute_action" not in offered
    assert not any(name.startswith("execute") for name in offered)


def test_the_model_can_only_prepare_an_action(conn, agent_context):
    from app.backend.models.actions import ActionStatus
    from app.backend.services.actions import get_ticket_escalations

    response, _ = run(
        conn,
        agent_context,
        "Escalate TKT-501.",
        [
            reply(
                calls=[
                    tool_call(
                        "c1",
                        "prepare_escalation",
                        '{"ticket_id": "TKT-501", "reason": "Full outage"}',
                    )
                ]
            ),
            reply(content="I have prepared an escalation for your confirmation."),
        ],
    )

    assert response.pending_action.status is ActionStatus.PENDING_CONFIRMATION
    assert get_ticket_escalations(conn, "TKT-501") == []


def test_a_prepared_action_is_labelled_as_not_yet_performed(conn, agent_context):
    _, client = run(
        conn,
        agent_context,
        "Escalate TKT-501.",
        [
            reply(
                calls=[
                    tool_call(
                        "c1",
                        "prepare_escalation",
                        '{"ticket_id": "TKT-501", "reason": "Full outage"}',
                    )
                ]
            ),
            reply(content="Prepared."),
        ],
    )

    feedback = [
        m for m in client.completions.calls[1]["messages"] if m["role"] == "tool"
    ][0]["content"]
    assert "PREPARED ONLY" in feedback


def test_the_step_budget_bounds_a_model_that_keeps_calling_tools(conn, agent_context):
    provider, _ = make_provider(
        [
            reply(calls=[tool_call(f"c{i}", "lookup_record", '{"entity": "dataset_metadata"}')])
            for i in range(10)
        ]
    )
    orchestrator = AgentOrchestrator(conn, provider=provider, max_steps=3)

    response = orchestrator.handle(AgentRequest(message="loop", context=agent_context))

    assert len(response.tool_invocations) == 3
    assert response.step_budget_exhausted is True


# --- what the model is shown ------------------------------------------------------------


def test_a_failed_tool_looks_failed_to_the_model():
    serialised = serialise_tool_result(
        ToolResult(status=ToolStatus.NOT_FOUND, message="order 'ORD-9999' was not found")
    )

    assert '"status": "not_found"' in serialised
    assert "ORD-9999" in serialised


def test_evidence_reaches_the_model_with_its_citation(conn, agent_context):
    registry = build_default_registry()
    result = registry.execute(
        conn, agent_context, "search_documents", {"query": "cancellation fee"}
    )

    serialised = serialise_tool_result(result)

    assert "citation" in serialised
    assert "is_deprecated" in serialised
    assert ".pdf" in serialised


def test_precedence_reaches_the_model_already_resolved(conn, agent_context):
    """The model is told which evidence governs; it does not decide that."""
    registry = build_default_registry()
    result = registry.execute(
        conn,
        agent_context,
        "search_documents",
        {"query": "cancellation fee", "account_id": NORTHSTAR_ACCOUNT},
    )

    import json

    payload = json.loads(serialise_tool_result(result))

    assert any(item["governing"] for item in payload["evidence"])
    assert any(not item["governing"] for item in payload["evidence"])
    assert payload["data"]["overrides"]


def test_chunk_text_is_not_sent_to_the_model_twice(conn, agent_context):
    """`data` and `evidence` used to carry the same bodies — a retrieval call
    would spend the context window twice on identical text."""
    registry = build_default_registry()
    result = registry.execute(
        conn, agent_context, "search_documents", {"query": "cancellation fee"}
    )

    import json

    payload = json.loads(serialise_tool_result(result))
    body = result.evidence[0].text

    def occurrences(node):
        if isinstance(node, str):
            return 1 if body in node else 0
        if isinstance(node, dict):
            return sum(occurrences(v) for v in node.values())
        if isinstance(node, list):
            return sum(occurrences(v) for v in node)
        return 0

    assert occurrences(payload) == 1
    assert "governing_chunk_ids" in payload["data"]


def test_an_oversized_tool_result_is_visibly_truncated(monkeypatch):
    import app.backend.agent.openai_provider as module

    monkeypatch.setattr(module, "MAX_TOOL_RESULT_CHARS", 50)
    serialised = module.serialise_tool_result(
        ToolResult(status=ToolStatus.OK, data={"blob": "x" * 500})
    )

    assert '"truncated": true' in serialised


def test_the_context_block_states_the_callers_scope(northstar_context):
    from app.backend.agent.prompts import build_context_block

    block = build_context_block(northstar_context)

    assert NORTHSTAR_ACCOUNT in block
    assert "support_agent" in block


def test_the_context_block_marks_an_external_customer(conn):
    from app.backend.agent.prompts import build_context_block
    from app.backend.models.agent import AgentContext, Role

    block = build_context_block(
        AgentContext(
            user_id="c", role=Role.CUSTOMER, allowed_account_ids=frozenset({"ACCT-001"})
        )
    )

    assert "external customer" in block


# --- the factory -------------------------------------------------------------------------


def test_the_factory_returns_the_deterministic_planner_by_default(api_settings):
    from app.backend.agent.factory import build_provider
    from app.backend.agent.provider import DeterministicPlanner

    assert isinstance(build_provider(api_settings), DeterministicPlanner)


def test_the_factory_builds_the_real_provider_when_configured(api_settings):
    from app.backend.agent.factory import build_provider
    from app.backend.core.config import ProviderMode

    configured = api_settings.model_copy(
        update={"provider_mode": ProviderMode.REAL, "openai_api_key": "sk-test-not-real"}
    )

    provider = build_provider(configured)

    assert isinstance(provider, OpenAIPlanningProvider)
    assert provider._model == configured.openai_model


def test_the_real_provider_satisfies_the_phase_4_seam():
    """Structural proof the abstraction was not bent to fit the model.

    The real provider must implement `next_step` with exactly the signature
    the Phase 4 protocol declares — if integrating a live model had required
    widening the seam, that would show up here.
    """
    import inspect

    from app.backend.agent.provider import DeterministicPlanner, PlanningProvider

    expected = inspect.signature(PlanningProvider.next_step)
    assert inspect.signature(OpenAIPlanningProvider.next_step) == expected
    assert inspect.signature(DeterministicPlanner.next_step) == expected
