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


PolicyDecision = CancellationDecision | ServiceCreditDecision


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
