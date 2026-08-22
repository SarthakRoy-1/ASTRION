"""Choosing and building the planning provider (Phase 5).

One function decides which implementation of the Phase 4 `PlanningProvider`
seam backs a request, and it decides from configuration alone:

    LLM_PROVIDER=deterministic  ->  DeterministicPlanner  (no key, no network)
    LLM_PROVIDER=real           ->  OpenAIPlanningProvider

There is deliberately **no fallback path** from `real` to `deterministic`. A
deployment that asked for a live model and quietly got a rule-based planner
would be running a different system than its operator believes, and the
difference would only surface in an answer nobody could explain. A missing key
is a startup failure; a provider outage at request time is a structured 502.

The deterministic planner is not a stub kept around for tests. It is the
reason every safety boundary in this system — scoping, precedence, policy
arithmetic, the confirmation gate — is verifiable without an API key, and it
stays a first-class supported mode.
"""

from __future__ import annotations

from datetime import datetime

from app.backend.agent.provider import DeterministicPlanner, PlanningProvider
from app.backend.core.config import ProviderMode, Settings
from app.backend.core.errors import ProviderConfigurationError


def build_provider(
    settings: Settings, *, reference_time: datetime | None = None
) -> PlanningProvider:
    """Construct the provider this request should run under.

    A fresh instance per request: the real provider accumulates the
    conversation transcript it is building, so sharing one across requests
    would mix conversations together.
    """
    if settings.provider_mode is ProviderMode.DETERMINISTIC:
        return DeterministicPlanner()

    settings.validate_provider()
    return build_openai_provider(settings, reference_time=reference_time)


def build_openai_provider(
    settings: Settings, *, reference_time: datetime | None = None
) -> PlanningProvider:
    """Build the OpenAI-backed provider, importing the SDK only when needed.

    The import is local so the whole application — and the whole test suite —
    runs on a machine that has never installed the vendor SDK. That is the
    practical proof that the abstraction is real rather than decorative.
    """
    from app.backend.agent.openai_provider import OpenAIPlanningProvider

    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ProviderConfigurationError(
            "LLM_PROVIDER=real requires the `openai` package. Install it with "
            "`pip install -r requirements.txt`, or use LLM_PROVIDER=deterministic."
        ) from exc

    client = OpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url or None,
        timeout=settings.agent_request_timeout_seconds,
        max_retries=1,
    )
    return OpenAIPlanningProvider(
        client=client,
        model=settings.openai_model,
        temperature=settings.llm_temperature,
        reference_time=reference_time,
    )
