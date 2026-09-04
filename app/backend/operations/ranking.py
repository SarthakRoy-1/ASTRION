"""Deterministic ranking of operational signals.

Detection answers "what is happening". Ranking answers "what first", and it is
the harder question: a list of six equally-presented concerns is barely more
useful than no list at all.

**The score is additive, small, and fully itemised.** Every contribution is a
named `PriorityFactor` carrying its own points and the observation that earned
them, so the ordering can be *read* rather than trusted:

    SLA breached, all severities        40   "breached against every target"
    Cross-customer (3 accounts)         30   "3 accounts affected"
    ...                                ───
    total                               70

That is the whole design. There is deliberately no weighting matrix, no
normalisation and no tuning: a number nobody can reconstruct is worse than an
arbitrary order, because it invites the reader to believe it means something.

**Confidence lowers priority; it never raises it.** A signal the detector could
not settle is multiplied down, so a probable-but-unverified concern cannot
outrank a confirmed one of equal size. The alternative — ranking on raw impact
and letting the reader notice the confidence badge — puts the least reliable
items at the top of the page, which is exactly where they do the most damage.
"""

from __future__ import annotations

from app.backend.agent.trust import TrustStatus
from app.backend.models.signals import (
    PriorityFactor,
    Signal,
    SignalSeverity,
    SignalType,
)

# --- factor weights ---------------------------------------------------------
#
# Chosen so the ordering matches how a support lead would triage, and stated
# here rather than buried in the code that applies them.

#: Detector severity. The dominant term: a critical signal should outrank a
#: medium one before anything else is considered.
SEVERITY_POINTS: dict[SignalSeverity, int] = {
    SignalSeverity.CRITICAL: 40,
    SignalSeverity.HIGH: 25,
    SignalSeverity.MEDIUM: 12,
    SignalSeverity.LOW: 4,
}

#: Points per additional affected account, beyond the first. A problem that
#: crosses customers is an operations concern rather than a support ticket,
#: and this is what encodes that.
#:
#: **Breadth amplifies severity; it cannot substitute for it.** The bonus is
#: additionally capped at the signal's own severity points, so a low-severity
#: observation spread across many accounts can at most double its own weight
#: and can never overtake a genuine breach. Without that cap, "4 of 6 orders
#: carry a cancellation request" — a volume observation with no established
#: cause — outranked a first-response target that may already be breached,
#: which is precisely the wrong thing to put at the top of a triage list.
POINTS_PER_EXTRA_ACCOUNT = 10
MAX_ACCOUNT_POINTS = 30

#: Points per affected record, capped. Volume matters, but it must not let a
#: large low-severity cluster outrank a genuine breach — hence the cap.
POINTS_PER_RECORD = 2
MAX_RECORD_POINTS = 10

#: A signal type that inherently demands faster action gets a flat bonus.
TYPE_POINTS: dict[SignalType, int] = {
    SignalType.SLA_RISK: 15,
    SignalType.CROSS_CUSTOMER_ISSUE: 10,
    SignalType.RECURRING_ISSUE: 5,
    SignalType.OPERATIONAL_ANOMALY: 0,
}

#: Matching documented material is worth a small *negative* adjustment: a
#: problem the product team has already written up and is tracking needs less
#: investigation than an unexplained one of the same size. Small, because a
#: known issue is not a resolved issue.
KNOWN_ISSUE_ADJUSTMENT = -5

#: Confidence multipliers, applied last. Percentages so the arithmetic stays
#: integer and the itemisation stays readable.
TRUST_MULTIPLIER: dict[str, int] = {
    TrustStatus.CONFIDENT.value: 100,
    TrustStatus.CONDITIONAL.value: 75,
    TrustStatus.INSUFFICIENT_DATA.value: 50,
    TrustStatus.CONFLICT.value: 60,
    # An escalation is not doubt about whether the concern is real — it is a
    # statement that a person must handle it. It keeps its full weight.
    TrustStatus.ESCALATE.value: 100,
}


def score(signal: Signal) -> tuple[int, list[PriorityFactor]]:
    """Compute one signal's priority and the factors that produced it."""
    factors: list[PriorityFactor] = []

    points = SEVERITY_POINTS.get(signal.severity, 0)
    factors.append(
        PriorityFactor(
            name="severity",
            points=points,
            basis=f"detector severity is {signal.severity.value}",
        )
    )

    severity_points = points
    extra_accounts = max(0, signal.affected_account_count - 1)
    if extra_accounts:
        account_points = min(
            extra_accounts * POINTS_PER_EXTRA_ACCOUNT,
            MAX_ACCOUNT_POINTS,
            # Breadth amplifies severity rather than replacing it.
            severity_points,
        )
        points += account_points
        factors.append(
            PriorityFactor(
                name="affected_accounts",
                points=account_points,
                basis=(
                    f"{signal.affected_account_count} accounts affected "
                    f"(capped at this signal's own severity weight)"
                ),
            )
        )

    record_count = len(signal.record_refs)
    if record_count:
        record_points = min(record_count * POINTS_PER_RECORD, MAX_RECORD_POINTS)
        points += record_points
        factors.append(
            PriorityFactor(
                name="affected_records",
                points=record_points,
                basis=(
                    f"{signal.affected_ticket_count} ticket(s), "
                    f"{signal.affected_order_count} order(s)"
                ),
            )
        )

    type_points = TYPE_POINTS.get(signal.signal_type, 0)
    if type_points:
        points += type_points
        factors.append(
            PriorityFactor(
                name="signal_type",
                points=type_points,
                basis=f"{signal.signal_type.value} needs faster handling",
            )
        )

    if signal.evidence_chunk_ids:
        points += KNOWN_ISSUE_ADJUSTMENT
        factors.append(
            PriorityFactor(
                name="documented",
                points=KNOWN_ISSUE_ADJUSTMENT,
                basis=(
                    f"matches {len(signal.evidence_chunk_ids)} documented "
                    f"section(s), so it is already understood"
                ),
            )
        )

    multiplier = TRUST_MULTIPLIER.get(signal.trust_status, 100)
    if multiplier != 100:
        before = points
        points = (points * multiplier) // 100
        factors.append(
            PriorityFactor(
                name="confidence",
                points=points - before,
                basis=(
                    f"trust is {signal.trust_status}, so the score is scaled to "
                    f"{multiplier}% — an unverified concern must not outrank a "
                    f"confirmed one"
                ),
            )
        )

    return max(points, 0), factors


def rank(signals: list[Signal]) -> list[Signal]:
    """Score every signal and return them worst-first.

    Ties break on severity, then affected-account count, then signal id — all
    deterministic, so the same input always produces the same order. Without
    the final id tiebreak the list could reshuffle between identical runs,
    which would make the page appear to change when nothing had.
    """
    scored: list[Signal] = []
    for signal in signals:
        points, factors = score(signal)
        scored.append(
            signal.model_copy(
                update={"priority_score": points, "priority_factors": factors}
            )
        )

    from app.backend.models.signals import SEVERITY_WEIGHT

    return sorted(
        scored,
        key=lambda s: (
            -s.priority_score,
            -SEVERITY_WEIGHT[s.severity],
            -s.affected_account_count,
            s.signal_id,
        ),
    )
