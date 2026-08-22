"""A real, model-backed `PlanningProvider` (Phase 5).

Implements exactly the seam Phase 4 defined — `next_step` — against OpenAI's
chat-completions function calling. The orchestrator, the tool registry, the
policy engine and the action state machine are untouched by this file, which
is the point: swapping the planner changes *which* tools get called, never
what any of them is permitted to do.

Shape of the integration:

    registry.schemas()  ->  function definitions handed to the model verbatim
    model tool_calls    ->  ToolCall objects the orchestrator executes
    ToolResult          ->  a `tool` message fed back on the next step
    model content with
    no tool calls       ->  `final_answer`, which the orchestrator prefers over
                            the deterministic composer's prose

Four things this provider deliberately does *not* do:

- It never touches the database, the policies package, or the action service.
  It has no import path to any of them.
- It never sees or forwards `allowed_account_ids`. Scope is injected by the
  orchestrator from the `AgentContext`; a model-emitted scoping argument is
  rejected by the tool layer, not filtered here.
- It never decides an action executed. `confirm_action` is not in
  `registry.schemas()`, so there is no tool call it could emit to reach it.
- It never silently swallows a failure. An API error, a timeout, or an
  unusable response raises `ProviderError`, which becomes a structured API
  error rather than prose.

**One instance per request.** The provider keeps the transcript it is building
so multi-call assistant turns reconstruct faithfully; reusing an instance
across requests would mix conversations.
"""

from __future__ import annotations

import json
from typing import Any

from app.backend.agent.prompts import SYSTEM_INSTRUCTIONS, build_context_block
from app.backend.agent.provider import PlannerStep, StepRecord, ToolCall
from app.backend.core.errors import ProviderError, ProviderTimeoutError
from app.backend.models.agent import AgentContext, ToolResult
from app.backend.tools.base import ToolRegistry

#: How many consecutive unusable model replies (malformed arguments, empty
#: responses) to repair inside one `next_step` before giving up. Bounded so a
#: model stuck emitting garbage cannot spin.
MAX_REPAIR_ATTEMPTS = 2

#: Cap on how much of a tool result is serialised back to the model. Evidence
#: text and record dumps are small in this corpus, but the cap keeps one
#: pathological result from consuming the whole context window.
MAX_TOOL_RESULT_CHARS = 12_000


class OpenAIPlanningProvider:
    """Plans the agent's tool calls with a live OpenAI model."""

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        temperature: float = 0.0,
        reference_time=None,
        max_repair_attempts: int = MAX_REPAIR_ATTEMPTS,
    ) -> None:
        self._client = client
        self._model = model
        self._temperature = temperature
        self._reference_time = reference_time
        self._max_repair_attempts = max_repair_attempts

        self._messages: list[dict] = []
        self._consumed = 0  # history entries already turned into tool messages
        self._pending_call_ids: list[str] = []
        self._call_counter = 0

        #: Set when the model answers instead of calling a tool. The
        #: orchestrator reads this attribute and prefers it over the
        #: deterministic composer's text.
        self.final_answer: str | None = None

    # --- PlanningProvider -----------------------------------------------------

    def next_step(
        self,
        message: str,
        context: AgentContext,
        history: list[StepRecord],
        registry: ToolRegistry,
    ) -> PlannerStep:
        if not self._messages:
            self._messages = [
                {"role": "system", "content": SYSTEM_INSTRUCTIONS},
                {
                    "role": "system",
                    "content": build_context_block(
                        context, reference_time=self._reference_time
                    ),
                },
                {"role": "user", "content": message},
            ]

        self._absorb(history)

        tools = [
            {"type": "function", "function": schema} for schema in registry.schemas()
        ]

        for attempt in range(self._max_repair_attempts + 1):
            choice = self._complete(tools)
            calls, malformed = self._read_tool_calls(choice)

            if malformed and not calls:
                if attempt >= self._max_repair_attempts:
                    raise ProviderError(
                        "The language model repeatedly produced tool calls that "
                        "could not be parsed.",
                        details={"malformed_calls": malformed},
                    )
                continue  # `_read_tool_calls` already queued the repair message

            if calls:
                return PlannerStep(calls)

            content = (choice.get("content") or "").strip()
            if content:
                self.final_answer = content
                self._messages.append({"role": "assistant", "content": content})
                return PlannerStep()

            if attempt >= self._max_repair_attempts:
                raise ProviderError(
                    "The language model returned neither an answer nor a tool call."
                )
            self._messages.append(
                {
                    "role": "user",
                    "content": (
                        "Your last reply was empty. Either call a tool or give the "
                        "final answer based on the tool results so far."
                    ),
                }
            )

        raise ProviderError("The language model could not produce a usable step.")

    # --- transcript management -------------------------------------------------

    def _absorb(self, history: list[StepRecord]) -> None:
        """Turn newly executed tool results into `tool` messages.

        The orchestrator executes exactly the calls this provider emitted, in
        order, so pairing new history entries with the ids assigned when they
        were planned is exact rather than heuristic.
        """
        for record in history[self._consumed :]:
            call_id = (
                self._pending_call_ids.pop(0)
                if self._pending_call_ids
                else f"call_{self._call_counter}"
            )
            self._messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": serialise_tool_result(record.result),
                }
            )
        self._consumed = len(history)

    def _read_tool_calls(self, choice: dict) -> tuple[list[ToolCall], list[dict]]:
        """Translate the model's tool calls, queueing repairs for bad ones.

        A tool call with unparseable arguments is *not* forwarded to the
        registry: the registry would report a type error that tells the model
        nothing useful. Instead the model is told exactly what failed to parse
        and asked again, which is how a real transcript recovers.
        """
        raw_calls = choice.get("tool_calls") or []
        if not raw_calls:
            return [], []

        assistant_message: dict = {"role": "assistant", "content": choice.get("content")}
        assistant_message["tool_calls"] = [
            {
                "id": raw["id"],
                "type": "function",
                "function": {
                    "name": raw["function"]["name"],
                    "arguments": raw["function"]["arguments"],
                },
            }
            for raw in raw_calls
        ]
        self._messages.append(assistant_message)

        calls: list[ToolCall] = []
        malformed: list[dict] = []
        call_ids: list[str] = []

        for raw in raw_calls:
            name = raw["function"]["name"]
            raw_arguments = raw["function"].get("arguments") or "{}"
            try:
                arguments = json.loads(raw_arguments) if raw_arguments.strip() else {}
            except json.JSONDecodeError as exc:
                malformed.append({"tool": name, "error": str(exc)})
                self._queue_repair(
                    raw["id"],
                    f"Arguments for {name} were not valid JSON ({exc}). "
                    f"Re-issue the call with a valid JSON object.",
                )
                continue
            if not isinstance(arguments, dict):
                malformed.append({"tool": name, "error": "arguments were not an object"})
                self._queue_repair(
                    raw["id"],
                    f"Arguments for {name} must be a JSON object, not "
                    f"{type(arguments).__name__}.",
                )
                continue

            # An unknown tool name is deliberately forwarded to the registry:
            # it answers with the list of tools that do exist, which is the
            # most useful correction the model can receive.
            calls.append(ToolCall(name, arguments))
            call_ids.append(raw["id"])
            self._call_counter += 1

        self._pending_call_ids.extend(call_ids)
        return calls, malformed

    def _queue_repair(self, call_id: str, message: str) -> None:
        """Answer a malformed tool call with an error the model can act on."""
        self._messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps({"status": "invalid_input", "message": message}),
            }
        )

    # --- the one place the network is touched ------------------------------------

    def _complete(self, tools: list[dict]) -> dict:
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=self._messages,
                tools=tools,
                tool_choice="auto",
                temperature=self._temperature,
            )
        except Exception as exc:
            raise _translate(exc) from exc

        try:
            message = response.choices[0].message
        except (AttributeError, IndexError, TypeError) as exc:
            raise ProviderError(
                "The language model returned a response in an unexpected shape."
            ) from exc

        return {
            "content": getattr(message, "content", None),
            "tool_calls": [
                {
                    "id": call.id,
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in (getattr(message, "tool_calls", None) or [])
            ],
        }


def serialise_tool_result(result: ToolResult) -> str:
    """Render a `ToolResult` as the JSON the model sees.

    Status and message are always included, including for failures: a tool
    that failed must look failed to the model, or it will answer around the
    gap. Evidence is flattened to citation-bearing entries so the model can
    quote a source without being handed the whole chunk graph.
    """
    payload: dict[str, Any] = {"status": result.status.value}
    if result.message:
        payload["message"] = result.message
    if result.data:
        payload["data"] = _compact_data(result.data)
    if result.evidence:
        # `governing` comes from the retrieval layer's own precedence split.
        # The model is told which evidence decides and which is context; it is
        # not asked to work that out, because that would be a second authority
        # implementation living in a prompt.
        governing = {
            entry.get("chunk_id")
            for entry in (result.data.get("governing") or [])
            if isinstance(entry, dict)
        }
        payload["evidence"] = [
            {
                "chunk_id": item.chunk_id,
                "citation": item.citation,
                "source_file": item.source_file,
                "page": item.page_number,
                "section": item.section_path,
                "authority_tier": int(item.authority_tier),
                "is_authoritative": item.is_authoritative,
                "is_deprecated": item.is_deprecated,
                "governing": item.chunk_id in governing,
                "text": item.text,
            }
            for item in result.evidence
        ]
    if result.decisions:
        payload["policy_decisions"] = [
            decision.model_dump(mode="json") for decision in result.decisions
        ]
    if result.proposed_action:
        payload["proposed_action"] = result.proposed_action.model_dump(mode="json")
        payload["reminder"] = (
            "This action is PREPARED ONLY. It has not been performed and you "
            "cannot perform it. A human must confirm it through the API."
        )

    text = json.dumps(payload, default=str)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        # Truncating is safer than dropping: the model must still see the
        # status, and a visibly truncated result is not mistaken for a full one.
        return json.dumps(
            {
                "status": result.status.value,
                "message": result.message,
                "truncated": True,
                "partial": text[:MAX_TOOL_RESULT_CHARS],
            }
        )
    return text


#: Keys in a tool result's `data` that repeat, verbatim, what the `evidence`
#: block already carries. Sending both would spend the context window twice on
#: the same chunk text for every retrieval call.
_EVIDENCE_ECHO_KEYS = ("governing", "contextual", "evidence")


def _compact_data(data: dict) -> dict:
    """Drop the parts of `data` that duplicate the evidence block.

    The chunk ids are kept so nothing the model needs to reference disappears;
    only the repeated body text goes.
    """
    compact: dict[str, Any] = {}
    for key, value in data.items():
        if key in _EVIDENCE_ECHO_KEYS and isinstance(value, list):
            ids = [
                entry["chunk_id"]
                for entry in value
                if isinstance(entry, dict) and entry.get("chunk_id")
            ]
            if ids:
                compact[f"{key}_chunk_ids"] = ids
                continue
        compact[key] = value
    return compact


def _translate(exc: Exception) -> ProviderError:
    """Map an SDK exception onto this application's error vocabulary.

    Kept name-based so this module imports and tests without the `openai`
    package installed — the abstraction stays honest about not depending on a
    vendor SDK's type hierarchy.
    """
    name = type(exc).__name__
    if "Timeout" in name:
        return ProviderTimeoutError(
            "The language model did not respond in time. Try again, or use the "
            "deterministic provider."
        )
    if "AuthenticationError" in name or "PermissionDenied" in name:
        return ProviderError("The language model rejected the configured credentials.")
    if "RateLimit" in name:
        return ProviderError("The language model provider is rate limiting requests.")
    if "Connection" in name:
        return ProviderError("Could not reach the language model provider.")
    # The upstream message is not forwarded: an error string is not guaranteed
    # to be free of request detail, and the server log keeps the original.
    return ProviderError(f"The language model provider failed ({name}).")
