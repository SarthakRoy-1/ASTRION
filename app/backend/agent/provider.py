"""Planning provider abstraction, and a deterministic implementation.

The orchestrator talks to a `PlanningProvider`, never to a vendor SDK. Adding
an OpenAI-backed provider later means implementing `next_step` — passing
`registry.schemas()` as function definitions and translating tool calls back —
without touching the orchestrator, the tools, or the policy engine.

`DeterministicPlanner` is the implementation shipped in this phase. It plans
from entity identifiers and intent keywords found in the request, which keeps
the whole agent runnable and testable with no API key and no network, exactly
as the policy engine is. It is a genuine multi-step planner: each step is
chosen from what previous tool results returned, not from a fixed script.

What it deliberately does *not* do is answer anything itself. Every fact,
figure and citation in a response comes from a tool result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from app.backend.models.agent import AgentContext, ToolResult, ToolStatus
from app.backend.policies.terms import severity_stated_in
from app.backend.tools.base import ToolRegistry

_ORDER_ID = re.compile(r"\bORD-\d+\b", re.IGNORECASE)
_TICKET_ID = re.compile(r"\bTKT-\d+\b", re.IGNORECASE)
_ACCOUNT_ID = re.compile(r"\bACCT-\d+\b", re.IGNORECASE)


class Intent(StrEnum):
    CANCELLATION = "cancellation"
    SERVICE_CREDIT = "service_credit"
    SLA = "sla"
    ESCALATION = "escalation"
    #: "What should operations look at right now?" — a question about the
    #: workspace as a whole rather than about one named record.
    OPERATIONS = "operations"
    INVESTIGATION = "investigation"


# Support staff do not all use the same words for the same request, and an
# intent this planner fails to recognise degrades the answer to "here are the
# governing documents" instead of a computed decision. Each phrase below is an
# ordinary synonym for the operation, never a phrase lifted from an example
# question. Widening these is safe because an intent only ever fires against an
# order this request already resolved (see the evaluation step below), so the
# worst case is evaluating a policy for an order the user themselves named.
_INTENT_KEYWORDS: tuple[tuple[Intent, tuple[str, ...]], ...] = (
    (
        Intent.CANCELLATION,
        ("cancel", "cancellation", "call off", "called off", "calling off",
         "back out", "withdraw", "scrap"),
    ),
    (
        Intent.SERVICE_CREDIT,
        ("service credit", "credit", "refund", "compensat", "money back",
         "reimburse", "goodwill", "make good"),
    ),
    (
        Intent.SLA,
        (
            "sla",
            "response target",
            "first response",
            "first-response",
            "breach",
            "overdue",
            "response time",
            "responded",
            "severity",
        ),
    ),
    (Intent.ESCALATION, ("escalate", "escalation")),
    (
        Intent.OPERATIONS,
        # Deliberately narrow. An earlier draft included conversational
        # phrases like "right now" and "what is going on", which fire on
        # ordinary single-ticket questions — "TKT-504 still shows BOOKED,
        # what is going on?" is a question about one ticket, not a request
        # for a workspace sweep. Every phrase below is about the operation as
        # a whole rather than about a record.
        (
            "operations look",
            "needs attention",
            "need attention",
            "what should we look",
            "what should i look",
            "high priority",
            "spike",
            "recurring",
            "across customers",
            "multiple customers",
            "other customers",
            "customers are affected",
            "customers affected",
            "anomal",
            "unusual",
        ),
    ),
)

#: Signal ids as they appear in a request, so a follow-up question about one
#: signal reaches `investigate_signal` rather than starting a fresh listing.
_SIGNAL_ID = re.compile(r"\b(?:SLA|RECUR|XCUST|PICKUP|CANCEL)-[A-Za-z0-9_.-]+")


@dataclass
class ToolCall:
    tool_name: str
    arguments: dict = field(default_factory=dict)


@dataclass
class StepRecord:
    """One completed tool call and what it returned."""

    tool_name: str
    arguments: dict
    result: ToolResult


@dataclass
class PlannerStep:
    """What to do next. No tool calls means the investigation is finished."""

    tool_calls: list[ToolCall] = field(default_factory=list)
    final_answer: str | None = None

    @property
    def is_final(self) -> bool:
        return not self.tool_calls


class PlanningProvider(Protocol):
    """Chooses the next tool calls given the request and what is known so far."""

    def next_step(
        self,
        message: str,
        context: AgentContext,
        history: list[StepRecord],
        registry: ToolRegistry,
    ) -> PlannerStep: ...


def detect_intents(message: str) -> set[Intent]:
    lowered = message.lower()
    found = {
        intent
        for intent, keywords in _INTENT_KEYWORDS
        if any(keyword in lowered for keyword in keywords)
    }
    return found or {Intent.INVESTIGATION}


def extract_ids(message: str) -> dict[str, list[str]]:
    """Identifiers named in the request, upper-cased and de-duplicated.

    Order is preserved so planning is deterministic for a given request.
    """

    def unique(pattern: re.Pattern) -> list[str]:
        seen: list[str] = []
        for match in pattern.finditer(message):
            value = match.group(0).upper()
            if value not in seen:
                seen.append(value)
        return seen

    return {
        "orders": unique(_ORDER_ID),
        "tickets": unique(_TICKET_ID),
        "accounts": unique(_ACCOUNT_ID),
    }


class DeterministicPlanner:
    """Rule-based multi-step planner.

    Plans in phases, advancing only on what earlier steps actually returned:

      1. resolve every identifier named in the request;
      2. derive the account from those records (never from the request text,
         so a mentioned customer name cannot widen scope);
      3. run the policy tool the intent calls for;
      4. retrieve supporting documentation, scoped to the derived account;
      5. prepare an action if one was explicitly requested;
      6. stop.

    A phase is skipped when its inputs are missing — an unresolvable order
    simply yields no policy step, and the composer reports the gap rather than
    filling it.
    """

    max_steps: int = 8

    def next_step(
        self,
        message: str,
        context: AgentContext,
        history: list[StepRecord],
        registry: ToolRegistry,
    ) -> PlannerStep:
        if len(history) >= self.max_steps:
            return PlannerStep()

        ids = extract_ids(message)
        intents = detect_intents(message)
        called = {(step.tool_name, _key(step.arguments)) for step in history}

        # --- 1. resolve identifiers -------------------------------------
        for order_id in ids["orders"]:
            call = ToolCall("lookup_record", {"entity": "order", "order_id": order_id})
            if ("lookup_record", _key(call.arguments)) not in called:
                return PlannerStep([call])

        for ticket_id in ids["tickets"]:
            call = ToolCall("lookup_record", {"entity": "ticket", "ticket_id": ticket_id})
            if ("lookup_record", _key(call.arguments)) not in called:
                return PlannerStep([call])

        for account_id in ids["accounts"]:
            call = ToolCall("lookup_record", {"entity": "account", "account_id": account_id})
            if ("lookup_record", _key(call.arguments)) not in called:
                return PlannerStep([call])

        # --- 2. derive scope from what was actually found ----------------
        account_id = _derived_account(history)
        resolved_orders = _resolved_ids(history, "order", "order_id")
        resolved_tickets = _resolved_ids(history, "ticket", "ticket_id")

        # A ticket investigation benefits from the account's other orders —
        # this is where "are other shipments affected?" becomes answerable.
        if account_id and resolved_tickets and not ids["orders"]:
            call = ToolCall(
                "lookup_record", {"entity": "account_orders", "account_id": account_id}
            )
            if ("lookup_record", _key(call.arguments)) not in called:
                return PlannerStep([call])

        # --- 3. deterministic policy evaluation ---------------------------
        if Intent.CANCELLATION in intents:
            for order_id in resolved_orders:
                call = ToolCall("evaluate_cancellation", {"order_id": order_id})
                if ("evaluate_cancellation", _key(call.arguments)) not in called:
                    return PlannerStep([call])

        if Intent.SERVICE_CREDIT in intents:
            for order_id in resolved_orders:
                call = ToolCall("evaluate_service_credit", {"order_id": order_id})
                if ("evaluate_service_credit", _key(call.arguments)) not in called:
                    return PlannerStep([call])

        if Intent.SLA in intents:
            # A severity the request states is passed through; one it does not
            # state is left absent, and the tool declines to assert a breach.
            # The planner has no basis for judging severity itself.
            stated = severity_stated_in(message)
            for ticket_id in resolved_tickets:
                arguments: dict = {"ticket_id": ticket_id}
                if stated is not None:
                    arguments["severity"] = stated.value
                if ("evaluate_sla", _key(arguments)) not in called:
                    return PlannerStep([ToolCall("evaluate_sla", arguments)])

        # --- 3b. the reference clock, when the question is about it --------
        #
        # Every time-based answer is already measured against the snapshot and
        # every decision records it, but a question asked *directly* about the
        # clock has no record to resolve and would otherwise fall through to a
        # document search that cannot answer it.
        if _asks_about_the_snapshot(message):
            call = ToolCall("lookup_record", {"entity": "dataset_metadata"})
            if ("lookup_record", _key(call.arguments)) not in called:
                return PlannerStep([call])

        # --- 3c. operations intelligence -----------------------------------
        #
        # Placed before document retrieval because "what should we look at"
        # is answered from detected signals, not from policy text. A named
        # signal goes straight to its detail; an open question lists the
        # ranked set.
        named_signals = _SIGNAL_ID.findall(message)
        for signal_id in named_signals:
            arguments = {"signal_id": signal_id}
            if ("investigate_signal", _key(arguments)) not in called:
                return PlannerStep([ToolCall("investigate_signal", arguments)])

        # A workspace-wide sweep only when the request is not about a specific
        # record. Naming an order, ticket or account makes it a question about
        # *that* record, and answering it with a ranked list of unrelated
        # signals would bury the answer the user actually asked for. This guard
        # is structural: it holds however the keyword list later changes.
        names_a_record = any(ids[kind] for kind in ("orders", "tickets", "accounts"))
        if Intent.OPERATIONS in intents and not named_signals and not names_a_record:
            arguments = {}
            if ("get_operational_signals", _key(arguments)) not in called:
                return PlannerStep([ToolCall("get_operational_signals", arguments)])

        # --- 4. supporting documentation ----------------------------------
        search_args: dict = {"query": message}
        if account_id:
            search_args["account_id"] = account_id
        if ("search_documents", _key(search_args)) not in called:
            return PlannerStep([ToolCall("search_documents", search_args)])

        # --- 5. explicitly requested action preparation --------------------
        if Intent.ESCALATION in intents and resolved_tickets:
            for ticket_id in resolved_tickets:
                arguments = {
                    "ticket_id": ticket_id,
                    "reason": _escalation_reason(message, history),
                    "evidence_chunk_ids": _evidence_ids(history),
                }
                if ("prepare_escalation", _key(arguments)) not in called:
                    return PlannerStep([ToolCall("prepare_escalation", arguments)])

        return PlannerStep()


#: Phrasings that ask what "now" is for this dataset, rather than asking a
#: question that merely happens to involve time.
_SNAPSHOT_PHRASES = (
    "snapshot",
    "what time is it",
    "current time",
    "reference time",
    "as of when",
    "how current",
    "today's date",
)


def _asks_about_the_snapshot(message: str) -> bool:
    lowered = message.lower()
    return any(phrase in lowered for phrase in _SNAPSHOT_PHRASES)


def _key(arguments: dict) -> str:
    """Stable identity for a call, so the planner never repeats one."""
    return repr(sorted((k, repr(v)) for k, v in arguments.items()))


def _resolved_ids(history: list[StepRecord], entity: str, id_field: str) -> list[str]:
    """Ids that a lookup actually returned — not ids merely mentioned.

    Planning on resolved records is what keeps an out-of-scope or misspelled
    identifier from driving a policy evaluation.
    """
    found: list[str] = []
    for step in history:
        if step.tool_name != "lookup_record" or not step.result.ok:
            continue
        if step.result.data.get("entity") != entity:
            continue
        record = step.result.data.get("record") or {}
        value = record.get(id_field)
        if value and value not in found:
            found.append(value)
    return found


def _derived_account(history: list[StepRecord]) -> str | None:
    """The account implied by resolved records.

    Read from returned data rather than from the request, so scope always
    follows a record the caller was permitted to read.
    """
    for step in history:
        if step.tool_name != "lookup_record" or not step.result.ok:
            continue
        record = step.result.data.get("record") or {}
        account_id = record.get("account_id") or step.result.data.get("account_id")
        if account_id:
            return account_id
    return None


def _evidence_ids(history: list[StepRecord]) -> list[str]:
    seen: list[str] = []
    for step in history:
        for item in step.result.evidence:
            if item.chunk_id not in seen:
                seen.append(item.chunk_id)
    return seen[:10]


def _escalation_reason(message: str, history: list[StepRecord]) -> str:
    """A reason grounded in the request and any uncertainty already surfaced."""
    reasons = [
        step.result.message
        for step in history
        if step.result.status is ToolStatus.UNCERTAIN and step.result.message
    ]
    base = f"Escalation requested via support agent: {message.strip()}"
    if reasons:
        return f"{base} | Outstanding verification: {'; '.join(reasons)}"
    return base
