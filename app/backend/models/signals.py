"""Operational signals: what the detection engine produces.

A *signal* is a claim that something in a workspace's operational data deserves
a person's attention, together with everything needed to judge that claim
without re-deriving it: what was observed, which records it rests on, how
urgent it is, and what to do next.

Three properties this model is shaped to enforce:

- **Every signal carries its evidence.** `record_refs` names the tickets,
  orders and accounts the detector actually read. A signal with no evidence is
  not a signal, it is an opinion — `Signal` requires the list to be non-empty.

- **Detection is deterministic and explainable.** `detail` states, in the
  detector's own words, the rule that fired and the numbers behind it. A
  support lead should be able to read a signal and reconstruct exactly why it
  appeared. Nothing here is produced by a model.

- **Priority is derived, not asserted.** `priority_score` comes from
  `operations/ranking.py`, and `priority_factors` records each contribution by
  name and value, so the ordering is auditable rather than a mystery number.

`SignalType` deliberately stays small. Each member is a detector that this
dataset can actually support; inventing categories the supplied data cannot
evidence would produce empty sections in the UI and imply telemetry that does
not exist.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class SignalType(StrEnum):
    """What kind of operational concern this is."""

    #: A first-response target that is breached, or close to it.
    SLA_RISK = "sla_risk"
    #: The same problem reported more than once for one account.
    RECURRING_ISSUE = "recurring_issue"
    #: One problem visible across several accounts — the signal that turns a
    #: support ticket into an operations concern.
    CROSS_CUSTOMER_ISSUE = "cross_customer_issue"
    #: A measurable operational deviation: overdue pickups, a concentration of
    #: cancellations, a carrier-fault cluster.
    OPERATIONAL_ANOMALY = "operational_anomaly"


class SignalSeverity(StrEnum):
    """How bad this is if left alone.

    Distinct from ticket severity (P1/P2/P3), which is a human judgement about
    one ticket. This is the detector's own assessment of the *signal*, derived
    from the rule that fired.
    """

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


#: Ordering for ranking and display. Higher is more urgent.
SEVERITY_WEIGHT: dict[SignalSeverity, int] = {
    SignalSeverity.CRITICAL: 4,
    SignalSeverity.HIGH: 3,
    SignalSeverity.MEDIUM: 2,
    SignalSeverity.LOW: 1,
}


class RecordKind(StrEnum):
    ACCOUNT = "account"
    ORDER = "order"
    TICKET = "ticket"


class RecordRef(BaseModel):
    """One operational record a signal rests on.

    Deliberately an id and a label rather than the record itself. The signal
    list is a summary; a reader who wants the record fetches it through the
    ordinary scoped repository, which re-checks their access at that point
    rather than trusting a copy embedded here.
    """

    model_config = ConfigDict(frozen=True)

    kind: RecordKind
    record_id: str
    account_id: str | None = None
    #: Short human label — a ticket subject, an order's carrier and status.
    #: Never a full description: this is an index entry, not a data export.
    label: str | None = None


class PriorityFactor(BaseModel):
    """One named contribution to a signal's priority score.

    Recorded individually so the ranking can be explained rather than merely
    applied. "Why is this above that one?" must have an answer a support lead
    can read.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    #: What the factor contributed to the total.
    points: int
    #: The observation that produced it, e.g. "3 accounts affected".
    basis: str


class Signal(BaseModel):
    """One detected operational concern, with everything needed to judge it."""

    model_config = ConfigDict(frozen=True)

    signal_id: str
    signal_type: SignalType
    severity: SignalSeverity

    #: One line, stating the concern. Written by the detector from real values.
    title: str
    #: The rule that fired and the numbers behind it.
    detail: str

    affected_account_ids: list[str] = Field(default_factory=list)
    record_refs: list[RecordRef] = Field(default_factory=list)

    #: Earliest and latest observation the signal is drawn from, judged against
    #: the dataset snapshot rather than the wall clock — the same reference
    #: every other time-based decision in this system uses.
    first_observed_at: datetime | None = None
    last_observed_at: datetime | None = None

    #: Documentation the detector matched, as chunk ids. Populated when a
    #: cluster corresponds to documented material — a known issue, a policy
    #: clause. Empty is normal and is not a defect.
    evidence_chunk_ids: list[str] = Field(default_factory=list)

    #: Derived by `operations/ranking.py`. Never set by a detector directly.
    priority_score: int = 0
    priority_factors: list[PriorityFactor] = Field(default_factory=list)

    #: Phase 2 trust vocabulary, reused rather than parallelled. A signal whose
    #: underlying decision could not be settled — an SLA target that depends on
    #: a severity nobody has classified — is `conditional`, not `confident`.
    trust_status: str = "confident"
    trust_reasons: list[str] = Field(default_factory=list)

    #: What a person should do next. Advisory prose; it never triggers anything.
    #: Acting on a signal goes through the ordinary confirmation gate.
    recommended_next_step: str | None = None

    @property
    def affected_account_count(self) -> int:
        return len(self.affected_account_ids)

    @property
    def affected_ticket_count(self) -> int:
        return sum(1 for r in self.record_refs if r.kind is RecordKind.TICKET)

    @property
    def affected_order_count(self) -> int:
        return sum(1 for r in self.record_refs if r.kind is RecordKind.ORDER)

    @property
    def is_cross_customer(self) -> bool:
        return self.affected_account_count > 1


class SignalReport(BaseModel):
    """Every signal detected for one workspace, in priority order.

    Carries the reference time so a reader knows what "overdue" was measured
    against — the dataset snapshot, never the wall clock.
    """

    model_config = ConfigDict(frozen=True)

    signals: list[Signal] = Field(default_factory=list)
    #: The dataset snapshot every detector measured against.
    reference_time: datetime | None = None
    #: Accounts actually in scope when detection ran. Echoed so a caller can
    #: see the boundary the report was produced under.
    scope_account_ids: list[str] = Field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.signals)

    def of_type(self, signal_type: SignalType) -> list[Signal]:
        return [s for s in self.signals if s.signal_type is signal_type]

    @property
    def highest_severity(self) -> SignalSeverity | None:
        if not self.signals:
            return None
        return max(self.signals, key=lambda s: SEVERITY_WEIGHT[s.severity]).severity
