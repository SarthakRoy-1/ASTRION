"""The default tool set exposed to the agent.

Assembling the registry in one place makes the model's reachable surface
enumerable — and makes its *omissions* checkable. Notably absent, and
deliberately so: any tool that executes a prepared action. Execution lives on
`AgentOrchestrator.confirm_action`, outside every registry, so a model has no
path to it regardless of what it is asked or told.
"""

from __future__ import annotations

from app.backend.tools.action_tools import (
    PREPARE_ESCALATION_SPEC,
    PREPARE_TICKET_NOTE_SPEC,
)
from app.backend.tools.base import ToolRegistry
from app.backend.tools.document_tools import (
    GET_DOCUMENT_EVIDENCE_SPEC,
    SEARCH_DOCUMENTS_SPEC,
)
from app.backend.tools.policy_tools import (
    EVALUATE_CANCELLATION_SPEC,
    EVALUATE_SERVICE_CREDIT_SPEC,
)
from app.backend.tools.record_tools import LOOKUP_PROVENANCE_SPEC, LOOKUP_RECORD_SPEC

READ_ONLY_TOOL_SPECS = (
    # Tool A — document retrieval (Phase 3, wrapped)
    SEARCH_DOCUMENTS_SPEC,
    GET_DOCUMENT_EVIDENCE_SPEC,
    # Tool B — structured lookup (Phase 2, wrapped)
    LOOKUP_RECORD_SPEC,
    LOOKUP_PROVENANCE_SPEC,
    # Tool C — deterministic policy decisions
    EVALUATE_CANCELLATION_SPEC,
    EVALUATE_SERVICE_CREDIT_SPEC,
)

# Tool D — state-changing action *preparation* only. Still not execution:
# even with these registered, the strongest a model can do is propose.
STATE_CHANGING_TOOL_SPECS = (
    PREPARE_ESCALATION_SPEC,
    PREPARE_TICKET_NOTE_SPEC,
)

DEFAULT_TOOL_SPECS = (*READ_ONLY_TOOL_SPECS, *STATE_CHANGING_TOOL_SPECS)


def build_default_registry(*, include_state_changing: bool = True) -> ToolRegistry:
    """Assemble the model's reachable tool surface.

    `include_state_changing=False` is the `ENABLE_STATE_CHANGING_ACTIONS` kill
    switch: the preparation tools are not registered at all, so they are
    absent from `schemas()` and unreachable rather than merely refused. This is
    an *additional* control, not the confirmation gate — that gate holds
    regardless, because execution was never in any registry.
    """
    registry = ToolRegistry()
    specs = DEFAULT_TOOL_SPECS if include_state_changing else READ_ONLY_TOOL_SPECS
    for spec in specs:
        registry.register(spec)
    return registry
