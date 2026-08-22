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
from app.backend.tools.base import ToolRegistry

_ORDER_ID = re.compile(r"\bORD-\d+\b", re.IGNORECASE)
_TICKET_ID = re.compile(r"\bTKT-\d+\b", re.IGNORECASE)
_ACCOUNT_ID = re.compile(r"\bACCT-\d+\b", re.IGNORECASE)


class Intent(StrEnum):
    CANCELLATION = "cancellation"
    SERVICE_CREDIT = "service_credit"
    ESCALATION = "escalation"
    INVESTIGATION = "investigation"


_INTENT_KEYWORDS: tuple[tuple[Intent, tuple[str, ...]], ...] = (
    (Intent.CANCELLATION, ("cancel", "cancellation")),
    (Intent.SERVICE_CREDIT, ("service credit", "credit", "refund", "compensat")),
    (Intent.ESCALATION, ("escalate", "escalation")),
)


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
