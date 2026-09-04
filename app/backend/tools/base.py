"""Tool contracts and dispatch (Phase 4).

Tools are the only surface the orchestrator — and therefore, later, a language
model — can reach. Each one is a thin, validated wrapper: it parses its
arguments, applies the caller's scope, delegates to Phase 2/3/4 code, and
returns a typed `ToolResult`. No business logic lives here.

**Scoping is injected, never accepted.** A tool handler receives the
`AgentContext` separately from the model-proposed arguments, and takes its
`allowed_account_ids` only from that context. Argument names that could be
mistaken for authorization inputs are rejected outright, so there is no
argument a model can emit that widens what it may see — the guarantee holds
structurally rather than by prompt instruction.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

from app.backend.models.agent import AgentContext, ToolResult, ToolStatus

# Argument names a tool will never accept from the model. Authorization comes
# from AgentContext alone; a proposal carrying any of these is rejected rather
# than silently ignored, so an attempt to widen scope is visible in the audit
# trail instead of failing quietly.
RESERVED_ARGUMENT_NAMES = frozenset(
    {"allowed_account_ids", "allowed_accounts", "user_id", "role", "context", "conn"}
)

ToolHandler = Callable[[sqlite3.Connection, AgentContext, dict], ToolResult]


@dataclass(frozen=True)
class ToolSpec:
    """A tool's identity, its JSON-schema contract, and its implementation.

    `parameters` is a JSON Schema object suitable for handing to a
    function-calling model verbatim, so the contract the model sees and the
    contract the code enforces come from one definition.
    """

    name: str
    description: str
    parameters: dict
    handler: ToolHandler
    mutating: bool = False


@dataclass
class ToolRegistry:
    """The set of tools available for a given run.

    State-changing *execution* is deliberately absent from every registry: an
    action can be prepared through a tool, but confirming and executing it is
    a separate orchestrator entry point that a model cannot call. The
    capability simply is not in the model's reachable surface.
    """

    _tools: dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self) -> list[dict]:
        """Function-calling definitions, stable in order."""
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            }
            for spec in sorted(self._tools.values(), key=lambda s: s.name)
        ]

    def execute(
        self,
        conn: sqlite3.Connection,
        context: AgentContext,
        name: str,
        arguments: dict | None = None,
    ) -> ToolResult:
        """Dispatch one tool call under the caller's authorization context."""
        arguments = dict(arguments or {})

        spec = self.get(name)
        if spec is None:
            return ToolResult(
                status=ToolStatus.INVALID_INPUT,
                message=f"unknown tool {name!r}; available: {', '.join(self.names())}",
            )

        forbidden = sorted(set(arguments) & RESERVED_ARGUMENT_NAMES)
        if forbidden:
            return ToolResult(
                status=ToolStatus.FORBIDDEN,
                message=(
                    f"tool arguments may not include {', '.join(forbidden)}; "
                    f"authorization is supplied by the execution context"
                ),
            )

        # `mutating` means the tool *prepares* an action, never that it
        # executes one — execution is not in any registry. So the gate here is
        # the proposal permission, not the execution permission: a SUPPORT
        # member may draft an escalation that only an OPERATIONS member can
        # confirm. For a context built without an organisation the two
        # predicates are identical, so no Phase 4 caller changes behaviour.
        if spec.mutating and not context.may_propose_action:
            return ToolResult(
                status=ToolStatus.FORBIDDEN,
                message=f"role {context.role.value!r} may not prepare state-changing actions",
            )

        try:
            return spec.handler(conn, context, arguments)
        except Exception as exc:  # a tool bug must not become a confident answer
            return ToolResult(
                status=ToolStatus.ERROR,
                message=f"{name} failed: {type(exc).__name__}: {exc}",
            )


def require_str(arguments: dict, key: str) -> tuple[str | None, ToolResult | None]:
    """Read a required string argument, or produce the rejection to return."""
    value = arguments.get(key)
    if value is None or not isinstance(value, str) or not value.strip():
        return None, ToolResult(
            status=ToolStatus.INVALID_INPUT,
            message=f"argument {key!r} is required and must be a non-empty string",
        )
    return value.strip(), None
