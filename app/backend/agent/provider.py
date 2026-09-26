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
    #: An explicit instruction to *issue* a credit, as opposed to asking
    #: whether one is due. Kept separate from SERVICE_CREDIT deliberately:
    #: "is ORD-2002 eligible for a credit?" must evaluate and answer, not
    #: propose a payment nobody asked to make.
    ISSUE_CREDIT = "issue_credit"
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


#: An instruction to *issue* a credit, rather than a question about whether one
#: is due.
#:
#: A pattern rather than a phrase list, because the words people put between
#: the verb and the noun are unbounded — "issue the failed-pickup service
#: credit", "apply that goodwill credit". Bounded to 40 characters so the two
#: halves have to belong to the same clause.
#:
#: Deliberately verb-led. A bare "credit" is already `SERVICE_CREDIT`, which
#: evaluates and answers; only these verbs turn the request into a proposal.
#: "known issue" and "what is the issue" cannot match, because `issue` here is
#: only ever followed by a credit within the same clause.
_ISSUE_CREDIT_PATTERN = re.compile(
    r"\b(issue|apply|grant|raise|process|prepare|award|pay out|payout)\b"
    r"[^.?!]{0,40}?\bcredit\b",
    re.IGNORECASE,
)


def detect_intents(message: str) -> set[Intent]:
    lowered = message.lower()
    found = {
        intent
        for intent, keywords in _INTENT_KEYWORDS
        if any(keyword in lowered for keyword in keywords)
    }
    if _ISSUE_CREDIT_PATTERN.search(message):
        found.add(Intent.ISSUE_CREDIT)
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

        # The response clock is part of any ticket investigation, so it is read
        # whenever a ticket resolves, not only when the request uses a word like
        # "SLA". A severity the request states is passed through; one it does not
        # state is left absent, and the tool declines to assert a breach (the
        # planner has no basis for judging severity). When the request is not
        # itself about response times the reading is marked as background, so a
        # missing severity is not presented as an open question about, say, a
        # billing-contact ticket. A closed ticket has no first-response clock
        # running, so it is only evaluated when the request asks.
        stated = severity_stated_in(message)
        about_response_time = bool({Intent.SLA, Intent.ESCALATION} & intents)
        for record in _resolved_records(history, "ticket"):
            ticket_id = record.get("ticket_id")
            if not ticket_id:
                continue
            if Intent.SLA not in intents and _is_closed(record.get("status")):
                continue
            arguments = {"ticket_id": ticket_id}
            if stated is not None:
                arguments["severity"] = stated.value
            if not about_response_time:
                arguments["background"] = True
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
        #
        # Once a ticket has resolved, what it says is what the documentation
        # question is about ("Investigate TKT-502" names no feature at all), so
        # its subject and description join the request in the query. Only those
        # two fields: a historical resolution is context that may be wrong, and
        # searching on it would go looking for the documentation it contradicts.
        search_args: dict = {"query": _search_query(message, history)}
        if account_id:
            search_args["account_id"] = account_id
        if ("search_documents", _key(search_args)) not in called:
            return PlannerStep([ToolCall("search_documents", search_args)])

        # --- 5. explicitly requested action preparation --------------------
        if Intent.ESCALATION in intents and resolved_tickets:
            for ticket_id in resolved_tickets:
                reason, evidence_ids, severity = _grounded_escalation(
                    ticket_id, message, history
                )
                arguments = {
                    "ticket_id": ticket_id,
                    "reason": reason,
                    "evidence_chunk_ids": evidence_ids,
                }
                if severity is not None:
                    arguments["severity"] = severity
                if ("prepare_escalation", _key(arguments)) not in called:
                    return PlannerStep([ToolCall("prepare_escalation", arguments)])

        # A credit is only ever *prepared*, and only when the request asked for
        # one to be issued rather than asked whether one was due. The amount is
        # not passed: `prepare_service_credit` reads it from the policy engine
        # and refuses an argument that tries to supply one.
        if Intent.ISSUE_CREDIT in intents and resolved_orders:
            for order_id in resolved_orders:
                arguments = {
                    "order_id": order_id,
                    "evidence_chunk_ids": _evidence_ids(history),
                }
                if ("prepare_service_credit", _key(arguments)) not in called:
                    return PlannerStep(
                        [ToolCall("prepare_service_credit", arguments)]
                    )

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


_CLOSED_STATUSES = frozenset({"closed", "resolved", "solved", "done", "cancelled", "canceled"})


def _is_closed(status: str | None) -> bool:
    return (status or "").strip().lower() in _CLOSED_STATUSES


def _resolved_records(history: list[StepRecord], entity: str) -> list[dict]:
    """The records a successful lookup of `entity` returned, in the order found."""
    records: list[dict] = []
    for step in history:
        if step.tool_name != "lookup_record" or not step.result.ok:
            continue
        if step.result.data.get("entity") != entity:
            continue
        record = step.result.data.get("record")
        if isinstance(record, dict) and record not in records:
            records.append(record)
    return records


def _search_query(message: str, history: list[StepRecord]) -> str:
    """The request, plus the subject and description of every ticket it resolved."""
    parts = [message]
    for record in _resolved_records(history, "ticket"):
        text = " ".join(
            str(record[field]).strip()
            for field in ("subject", "description")
            if record.get(field)
        )
        if text:
            parts.append(text)
    return " ".join(parts)


def _sla_decisions(history: list[StepRecord], ticket_id: str) -> list:
    return [
        decision
        for step in history
        if step.tool_name == "evaluate_sla"
        for decision in step.result.decisions
        if getattr(decision, "ticket_id", None) == ticket_id
    ]


def _grounded_escalation(
    ticket_id: str, message: str, history: list[StepRecord]
) -> tuple[str, list[str], str | None]:
    """An escalation's reason, evidence and severity, taken from what was found.

    A reviewer confirming an escalation is entitled to see *why*, in terms of the
    policy and the records, not the operator's own sentence handed back to them.
    So the reason is assembled from the ticket's response-clock decision (a
    breach, or a policy definition the ticket resembles), the evidence cited is
    what that decision rested on, and the severity is recorded only when a person
    supplied it. The request is kept as context at the end, never as the ground.
    """
    ticket = next(
        (r for r in _resolved_records(history, "ticket") if r.get("ticket_id") == ticket_id),
        {},
    )
    facts: list[str] = []
    evidence: list[str] = []
    severity: str | None = None

    for decision in _sla_decisions(history, ticket_id):
        for chunk_id in decision.evidence_chunk_ids:
            if chunk_id not in evidence:
                evidence.append(chunk_id)
        if decision.breached is True:
            facts.append(
                f"{decision.severity.value} first-response target breached: "
                f"{decision.calculation}"
            )
        elif decision.severity_indications:
            indication = decision.severity_indications[0]
            fact = (
                f"the ticket's text matches the current policy's "
                f"{indication.severity.value} definition (\"{indication.criterion}\", "
                f"{indication.source}); severity is not yet verified"
            )
            if indication.target_text and decision.elapsed_minutes is not None:
                fact += (
                    f". If {indication.severity.value}, the account's target is "
                    f"{indication.target_text} and {decision.elapsed_minutes} minutes "
                    f"have elapsed"
                )
            facts.append(fact)
            if indication.chunk_id not in evidence:
                evidence.append(indication.chunk_id)
        elif decision.target_text is not None and decision.elapsed_minutes is not None:
            facts.append(
                f"first-response target {decision.target_text}, "
                f"{decision.elapsed_minutes} minutes elapsed"
            )
        if decision.severity is not None and decision.severity_source == "supplied by caller":
            severity = decision.severity.value
        if decision.requires_immediate_escalation:
            facts.append("the current policy requires P1 incidents to be escalated immediately")

    subject = ticket.get("subject")
    label = f"{ticket_id} ({subject})" if subject else ticket_id
    if facts:
        reason = f"Escalating {label}: " + "; ".join(facts) + "."
    else:
        status = ticket.get("status") or "unknown status"
        reason = (
            f"Escalating {label} ({status}). No response-clock finding supports it; "
            f"it rests on the operator's request alone."
        )
    reason += f" Requested: {message.strip()[:200]}"

    if not evidence:
        evidence = _evidence_ids(history)
    return reason, evidence, severity
