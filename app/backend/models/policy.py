"""Models for deterministic policy decisions (Phase 4).

Every monetary figure and every eligibility verdict a user could act on is
produced by code in `app/backend/policies/` and carried in these structures —
never composed by a language model. Each decision therefore ships with the
inputs it used, the rule that controlled it, and the evidence chunk ids
backing that rule, so the arithmetic can be reconstructed and audited.

Money is `Decimal`, never `float`: these are amounts that end up on invoices.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.backend.models.documents import AuthorityTier


class PolicyOutcome(StrEnum):
    """The verdict. REQUIRES_VERIFICATION is a first-class success, not a
    failure: the SOP explicitly forbids promising a credit when carrier
    fault, pickup timing, or customer fault is unknown."""

    ALLOWED = "allowed"
    NOT_ALLOWED = "not_allowed"
    ELIGIBLE = "eligible"
    NOT_ELIGIBLE = "not_eligible"
    REQUIRES_VERIFICATION = "requires_verification"


class TermSource(BaseModel):
    """Where one extracted policy parameter came from.

    Extraction reads authority-resolved document text, so a term always names
    the chunk that stated it. A term with no source was never stated by any
    document and must not be used.
    """

    model_config = ConfigDict(frozen=True)

    field: str
    value: str
    matched_text: str
    chunk_id: str
    source_file: str
    section_path: str | None
    authority_tier: AuthorityTier


class CancellationTerms(BaseModel):
    """Cancellation parameters assembled from evidence.

    Built by layering: defaults from the current SOP, then any governing
    customer agreement's terms overlaid on top. Nothing here is keyed to a
    named customer — see app/backend/policies/terms.py.
    """

    model_config = ConfigDict(frozen=True)

    free_window_minutes: int | None = None
    fee_amount: Decimal | None = None
    fee_waived: bool = False
    currency: str = "INR"
    sources: list[TermSource] = []

    @property
    def is_complete(self) -> bool:
        """Whether enough was extracted to decide a fee at all."""
        return self.fee_waived or (self.free_window_minutes is not None and self.fee_amount is not None)


class ServiceCreditTerms(BaseModel):
    """Failed-pickup service-credit parameters assembled from evidence."""

    model_config = ConfigDict(frozen=True)

    delay_threshold_hours: Decimal | None = None
    fixed_amount: Decimal | None = None
    percentage_of_fee: Decimal | None = None
    max_amount: Decimal | None = None
    monthly_cap: Decimal | None = None
    manager_approval_above: Decimal | None = None
    currency: str = "INR"
    sources: list[TermSource] = []

    @property
    def is_complete(self) -> bool:
        if self.delay_threshold_hours is None:
            return False
        return self.fixed_amount is not None or (
            self.percentage_of_fee is not None and self.max_amount is not None
        )


class Severity(StrEnum):
    """The severity levels the current support policy defines.

    Membership is closed on purpose: a severity outside this set has no
    response target anywhere in the corpus, so accepting one would produce a
    target that no document states.
    """

    P1 = "P1"
    P2 = "P2"
    P3 = "P3"

    @classmethod
    def parse(cls, raw: str | None) -> Severity | None:
        if not raw or not raw.strip():
            return None
        try:
            return cls(raw.strip().upper())
        except ValueError:
            return None


class ResponseTarget(BaseModel):
    """One first-response target as a document states it.

    `minutes` is populated only for a target expressed in clock time. A target
    stated in business hours keeps `minutes = None`, because the corpus defines
    no business calendar and a converted figure would be invented rather than
    read.
    """

    model_config = ConfigDict(frozen=True)

    severity: Severity
    text: str
    minutes: int | None = None
    is_business_time: bool = False
    source: TermSource | None = None


class ResponseTargets(BaseModel):
    """First-response targets assembled from evidence, layered by authority.

    Built the same way as the other term sets: SOP/policy defaults first, then
    any governing customer agreement overlaid on top, field by field.
    """

    model_config = ConfigDict(frozen=True)

    targets: dict[Severity, ResponseTarget] = {}
    escalate_p1_immediately: bool = False
    plan: str | None = None
    sources: list[TermSource] = []

    def for_severity(self, severity: Severity) -> ResponseTarget | None:
        return self.targets.get(severity)


class SlaDecision(BaseModel):
    """Result of evaluating a ticket's first-response target and breach state."""

    model_config = ConfigDict(frozen=True)

    decision_type: str = "sla"
    ticket_id: str
    account_id: str
    outcome: PolicyOutcome

    severity: Severity | None = None
    severity_source: str | None = None
    plan: str | None = None

    target_text: str | None = None
    target_minutes: int | None = None
    elapsed_minutes: Decimal | None = None
    breached: bool | None = None
    first_response_recorded: bool = False
    requires_immediate_escalation: bool = False

    controlling_rule: str
    controlling_sources: list[str]

    requires_verification: bool = False
    verification_reasons: list[str] = []

    inputs: dict[str, str | None] = {}
    calculation: str | None = None
    overrides: list[str] = []
    evidence_chunk_ids: list[str] = []
    targets: ResponseTargets | None = None


class PickupConfirmationLag(BaseModel):
    """A documented delay between a carrier collecting a parcel and ParcelPilot
    recording the pickup.

    Carries the carrier it applies to and the bound the documentation states,
    so a caller can ask the only question that matters: does this known issue
    actually explain *this* order's missing pickup confirmation, or has the
    documented window already elapsed?
    """

    model_config = ConfigDict(frozen=True)

    carrier: str
    lag_minutes: int
    source: TermSource


class CancellationDecision(BaseModel):
    """Result of evaluating whether an order may be cancelled, and at what fee."""

    model_config = ConfigDict(frozen=True)

    decision_type: str = "cancellation"
    order_id: str
    account_id: str
    outcome: PolicyOutcome
    can_cancel: bool
    fee_applies: bool
    fee_amount: Decimal | None
    currency: str

    controlling_rule: str
    controlling_sources: list[str]
    alternative_workflow: str | None = None

    requires_verification: bool = False
    verification_reasons: list[str] = []

    inputs: dict[str, str | None] = {}
    calculation: str | None = None
    overrides: list[str] = []
    evidence_chunk_ids: list[str] = []
    terms: CancellationTerms | None = None


class ServiceCreditDecision(BaseModel):
    """Result of evaluating failed-pickup service-credit eligibility."""

    model_config = ConfigDict(frozen=True)

    decision_type: str = "service_credit"
    order_id: str
    account_id: str
    outcome: PolicyOutcome

    eligible: bool
    credit_amount: Decimal | None
    provisional: bool = False
    currency: str

    delay_hours: Decimal | None = None
    threshold_hours: Decimal | None = None
    carrier_fault: bool | None = None
    customer_fault: bool | None = None
    pickup_confirmed: bool = False

    controlling_rule: str
    controlling_sources: list[str]

    requires_verification: bool = False
    verification_reasons: list[str] = []
    requires_manager_approval: bool = False
    monthly_cap: Decimal | None = None

    inputs: dict[str, str | None] = {}
    calculation: str | None = None
    overrides: list[str] = []
    evidence_chunk_ids: list[str] = []
    terms: ServiceCreditTerms | None = None


PolicyDecision = CancellationDecision | ServiceCreditDecision | SlaDecision


class PolicyEvaluationContext(BaseModel):
    """The reference clock a time-based policy question is evaluated against.

    Loaded from `dataset_metadata` rather than `datetime.now()`: the supplied
    dataset is a snapshot, and evaluating it against wall-clock time would
    make every SLA and cancellation-window answer drift daily.
    """

    model_config = ConfigDict(frozen=True)

    reference_time: datetime
    reference_time_source: str
    currency: str = "INR"
