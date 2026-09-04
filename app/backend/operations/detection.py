"""Deterministic detection of operational signals.

Every detector here is a rule over the supplied structured data, measured
against the dataset snapshot. There is no model, no statistical inference and
no telemetry: a support lead should be able to read a signal's `detail` and
reconstruct exactly why it appeared from the records themselves.

Four detectors, each chosen because the supplied dataset can actually evidence
it:

    SLA risk              first-response targets, agreement-aware
    Recurring issue       the same problem reported more than once
    Cross-customer issue  one problem visible across several accounts
    Operational anomaly   overdue pickups, cancellation concentration

**What is deliberately absent.** No ML anomaly detection, no trend
extrapolation, no forecasting. This dataset has six orders and seven tickets;
a statistical model over it would be unfalsifiable decoration, and its output
could not be explained to the person acting on it.

**Severity is not invented.** Tickets carry no severity column, and Phase 2
established that severity is a judgement about business impact rather than a
calculation. So the SLA detector compares elapsed time against *every*
computable target and reports what that does and does not settle — a breach is
asserted only when it holds regardless of which severity a human assigns.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection
from datetime import datetime
from decimal import Decimal

from app.backend.agent.trust import TrustStatus
from app.backend.models.records import Order, Ticket
from app.backend.models.signals import (
    RecordKind,
    RecordRef,
    Signal,
    SignalSeverity,
    SignalType,
)
from app.backend.policies.base import PolicyDataError, load_evaluation_context
from app.backend.policies.sla import PolicyLookupError, evaluate_sla
from app.backend.models.documents import Topic
from app.backend.retrieval.search import search_documents, tokenize
from app.backend.services import operations as ops

# --- tuning constants -------------------------------------------------------
#
# Every threshold is named, commented, and reported in the signal it produces,
# so a reader can see both the rule and the number it fired on.

#: A ticket is "approaching" its target once this much of the shortest
#: computable target has elapsed. 0.75 leaves a quarter of the window to act
#: in — enough to be useful, late enough not to flag every new ticket.
APPROACHING_FRACTION = Decimal("0.75")

#: Two tickets are the same problem when they share at least this many
#: distinctive terms. Three is deliberately conservative: two shared terms
#: ("shipment", "failing") happen by accident across unrelated tickets.
MIN_SHARED_TERMS = 3

#: Shared-term count at which a cluster is reported as settled rather than
#: probable.
#:
#: Lexical clustering over two-sentence support tickets has a real precision
#: limit, and pretending otherwise would be the failure this whole project is
#: built to avoid. A billing question about a BOOKED shipment and a webhook
#: delay on a BOOKED shipment genuinely share "booked", "pickup" and "minutes"
#: while being different problems.
#:
#: The response is to keep detecting them — a missed recurrence is worse than a
#: checked one — and to lower the *confidence* rather than raise the threshold.
#: A cluster held together by only a handful of generic operational words is
#: reported as `conditional` and says so, so the reader verifies instead of
#: trusting. Raising `MIN_SHARED_TERMS` until this corpus stopped producing
#: false positives would be fitting the rule to the sample.
CONFIDENT_SHARED_TERMS = MIN_SHARED_TERMS + 2

#: A term appearing in more than this fraction of tickets carries no
#: distinguishing information — it describes the product, not the problem.
#: Excluding them stops "shipment" clustering the entire corpus.
UBIQUITY_FRACTION = 0.6

#: A carrier must serve at least this many distinct accounts before a problem
#: with it is an operations concern rather than one customer's bad luck.
CROSS_CUSTOMER_MIN_ACCOUNTS = 2

#: Words that carry no problem-specific meaning in support text. Kept tiny;
#: the ubiquity filter above does most of the work, and a long hand-written
#: list is a maintenance burden that silently changes detection.
_NOISE = frozenset(
    {
        "the", "and", "for", "with", "still", "when", "any", "all", "can", "not",
        "but", "our", "their", "they", "this", "that", "from", "has", "have",
        "are", "was", "were", "get", "gets", "got", "customer", "customers",
        "user", "users", "please", "would", "could", "about", "after", "before",
        "parcelpilot", "account",
    }
)


def _terms(ticket: Ticket) -> set[str]:
    """Distinctive terms in a ticket, for clustering.

    Reuses the retrieval layer's tokenizer rather than writing a second one, so
    a ticket and a document are broken into terms the same way — which is what
    lets a cluster be matched against documentation later.
    """
    text = " ".join(filter(None, (ticket.subject, ticket.description)))
    return {t for t in tokenize(text) if len(t) > 2 and t not in _NOISE}


def _distinctive_terms(tickets: list[Ticket]) -> dict[str, set[str]]:
    """Per-ticket terms, with corpus-ubiquitous ones removed.

    A term in most tickets describes the product rather than the problem.
    Removing them is what stops every ticket clustering with every other.
    """
    raw = {t.ticket_id: _terms(t) for t in tickets}
    if not raw:
        return {}

    frequency: dict[str, int] = {}
    for terms in raw.values():
        for term in terms:
            frequency[term] = frequency.get(term, 0) + 1

    ceiling = max(1, int(len(raw) * UBIQUITY_FRACTION))
    return {
        ticket_id: {t for t in terms if frequency[t] <= ceiling}
        for ticket_id, terms in raw.items()
    }


def _cluster(tickets: list[Ticket]) -> list[list[Ticket]]:
    """Group tickets describing the same problem. Deterministic.

    Single-link agglomeration on shared distinctive terms: two tickets join the
    same cluster when they share at least `MIN_SHARED_TERMS`. Chosen over a
    similarity threshold because "shares three specific words" is a rule a
    support lead can check by reading the two tickets, and a cosine distance is
    not.

    Order is fixed by ticket id throughout, so the same input always yields the
    same clusters.
    """
    ordered = sorted(tickets, key=lambda t: t.ticket_id)
    terms = _distinctive_terms(ordered)

    clusters: list[list[Ticket]] = []
    for ticket in ordered:
        mine = terms.get(ticket.ticket_id, set())
        placed = False
        for cluster in clusters:
            if any(
                len(mine & terms.get(other.ticket_id, set())) >= MIN_SHARED_TERMS
                for other in cluster
            ):
                cluster.append(ticket)
                placed = True
                break
        if not placed:
            clusters.append([ticket])
    return [c for c in clusters if len(c) > 1]


def _shared_terms(cluster: list[Ticket], terms: dict[str, set[str]]) -> list[str]:
    """The terms every ticket in a cluster has in common, for explanation."""
    sets = [terms.get(t.ticket_id, set()) for t in cluster]
    if not sets:
        return []
    common = set.intersection(*sets) if len(sets) > 1 else sets[0]
    return sorted(common)


def _ticket_ref(ticket: Ticket) -> RecordRef:
    return RecordRef(
        kind=RecordKind.TICKET,
        record_id=ticket.ticket_id,
        account_id=ticket.account_id,
        label=ticket.subject,
    )


def _order_ref(order: Order) -> RecordRef:
    return RecordRef(
        kind=RecordKind.ORDER,
        record_id=order.order_id,
        account_id=order.account_id,
        label=f"{order.carrier or 'unknown carrier'} / {order.status or 'unknown status'}",
    )


def _signal_id(prefix: str, *parts: str) -> str:
    """A stable id derived from what the signal is about.

    Deterministic rather than random: the same signal detected twice must carry
    the same id, or a UI cannot tell "still happening" from "happened again".
    """
    return "-".join([prefix, *sorted(parts)])


def correlate_known_issues(
    conn: sqlite3.Connection,
    terms: list[str],
    *,
    allowed_account_ids: Collection[str] | None,
    limit: int = 3,
) -> list[str]:
    """Documentation chunk ids matching a detected cluster's shared terms.

    Correlation, not detection: the cluster was found in the *ticket data*, and
    this only asks whether the product documentation already describes it. That
    ordering matters — keying detection off a hard-coded list of known-issue ids
    would find only problems somebody had already written down, which is the
    opposite of proactive.

    Runs through the ordinary scoped search, so it can only ever read
    documentation this caller is permitted to see, and only in-force material is
    considered: a resolved issue must not be offered as the explanation for a
    live one.
    """
    if not terms:
        return []
    try:
        matches = search_documents(
            conn,
            " ".join(terms),
            allowed_account_ids=allowed_account_ids,
            include_non_authoritative=False,
            limit=limit,
        )
    except Exception:  # retrieval is advisory here; a failure must not lose the signal
        return []
    return [
        item.chunk_id
        for item in matches
        if item.topic is Topic.PRODUCT_KNOWN_ISSUES
    ]


# --- detectors --------------------------------------------------------------


def detect_sla_risk(
    conn: sqlite3.Connection,
    tickets: list[Ticket],
    *,
    allowed_account_ids: Collection[str] | None,
) -> list[Signal]:
    """First-response targets that are breached or close to it.

    Reuses `evaluate_sla`, so the targets are the ones that actually govern
    this account — a customer agreement's tighter target overrides the default
    policy automatically, and this detector neither knows nor needs to know
    that it happened.

    The severity problem, and how it is handled honestly: a ticket has no
    severity, and severity is a human judgement. So elapsed time is compared
    against *every computable* target:

        elapsed > every target   -> breached whichever severity applies (CONFIDENT)
        elapsed > some target    -> breached only if severity is high (CONDITIONAL)
        elapsed > 75% of nearest -> approaching (CONDITIONAL)

    Targets expressed in business hours have no fixed minute count and are
    excluded from the comparison rather than guessed at; the signal says so.
    """
    signals: list[Signal] = []

    for ticket in tickets:
        try:
            decision = evaluate_sla(
                conn, ticket.ticket_id, allowed_account_ids=allowed_account_ids
            )
        except (PolicyLookupError, PolicyDataError):
            # Out of scope or unevaluable. Not a signal, and emphatically not
            # an error to surface: the scoped repository already decided this
            # caller may not see it.
            continue

        if decision.first_response_recorded:
            continue

        # `decision.targets` is a `ResponseTargets` wrapper whose `.targets`
        # holds the per-severity mapping, already layered by authority — a
        # customer agreement's tighter target has overlaid the default policy
        # by the time it reaches here.
        band_targets = decision.targets.targets if decision.targets else {}
        computable = {
            severity: target.minutes
            for severity, target in band_targets.items()
            if target.minutes is not None
        }
        if not computable or decision.elapsed_minutes is None:
            continue

        elapsed = Decimal(str(decision.elapsed_minutes))
        shortest = min(computable.values())
        longest = max(computable.values())
        bands = ", ".join(
            f"{sev.value} {minutes}m" for sev, minutes in sorted(computable.items())
        )
        uncomputed = [
            sev.value for sev, target in band_targets.items() if target.minutes is None
        ]
        caveat = (
            f" Targets for {', '.join(sorted(uncomputed))} are expressed in "
            f"business hours and are not compared here."
            if uncomputed
            else ""
        )

        if elapsed > longest:
            severity, trust = SignalSeverity.CRITICAL, TrustStatus.CONFIDENT
            detail = (
                f"No first response after {elapsed} minutes, which exceeds every "
                f"computable first-response target for this account ({bands}). "
                f"This is breached whichever severity the ticket is assigned."
                f"{caveat}"
            )
            reasons: list[str] = []
            step = "Respond now and escalate; the target is already missed."
        elif elapsed > shortest:
            severity, trust = SignalSeverity.HIGH, TrustStatus.CONDITIONAL
            missed = sorted(
                sev.value for sev, minutes in computable.items() if elapsed > minutes
            )
            detail = (
                f"No first response after {elapsed} minutes. That is past the "
                f"{', '.join(missed)} target for this account ({bands}), so it is "
                f"breached if the ticket is {' or '.join(missed)} — and within "
                f"target otherwise.{caveat}"
            )
            reasons = [
                "Severity has not been classified, so whether this is breached "
                "depends on a judgement nobody has made yet."
            ]
            step = (
                "Classify the ticket against the current policy's severity "
                "definitions, then re-evaluate."
            )
        elif elapsed > shortest * APPROACHING_FRACTION:
            severity, trust = SignalSeverity.MEDIUM, TrustStatus.CONDITIONAL
            detail = (
                f"No first response after {elapsed} minutes, approaching the "
                f"tightest target for this account ({shortest} minutes). "
                f"Targets: {bands}.{caveat}"
            )
            reasons = ["Severity has not been classified."]
            step = "Respond before the tightest applicable target elapses."
        else:
            continue

        signals.append(
            Signal(
                signal_id=_signal_id("SLA", ticket.ticket_id),
                signal_type=SignalType.SLA_RISK,
                severity=severity,
                title=f"{ticket.ticket_id} has no first response after {elapsed} minutes",
                detail=detail,
                affected_account_ids=[ticket.account_id],
                record_refs=[_ticket_ref(ticket)],
                first_observed_at=ticket.created_at,
                last_observed_at=ticket.last_customer_message_at or ticket.created_at,
                evidence_chunk_ids=list(decision.evidence_chunk_ids or []),
                trust_status=trust.value,
                trust_reasons=reasons,
                recommended_next_step=step,
            )
        )
    return signals


def detect_recurring_and_cross_customer(
    conn: sqlite3.Connection,
    tickets: list[Ticket],
    names: dict[str, str],
    *,
    allowed_account_ids: Collection[str] | None = None,
) -> list[Signal]:
    """Repeated problems, and problems spanning several accounts.

    One clustering pass produces both: a cluster confined to one account is a
    *recurring* issue for that customer; a cluster spanning several is a
    *cross-customer* issue, which is the signal that turns a support ticket
    into an operations concern.
    """
    signals: list[Signal] = []
    terms = _distinctive_terms(sorted(tickets, key=lambda t: t.ticket_id))

    for cluster in _cluster(tickets):
        accounts = sorted({t.account_id for t in cluster})
        shared = _shared_terms(cluster, terms)
        ids = sorted(t.ticket_id for t in cluster)
        observed = [t.created_at for t in cluster if t.created_at]
        cross = len(accounts) > 1
        known_issue_chunks = correlate_known_issues(
            conn, shared, allowed_account_ids=allowed_account_ids
        )

        # Evidence strength decides confidence, not whether to report.
        strong = len(shared) >= CONFIDENT_SHARED_TERMS
        weak_reason = (
            f"These tickets were grouped on {len(shared)} shared terms "
            f"({', '.join(shared)}), which may be generic operational vocabulary "
            f"rather than the same underlying problem. Read both before acting."
        )

        if cross:
            who = ", ".join(names.get(a, a) for a in accounts)
            signals.append(
                Signal(
                    signal_id=_signal_id("XCUST", *ids),
                    signal_type=SignalType.CROSS_CUSTOMER_ISSUE,
                    severity=(
                        SignalSeverity.HIGH if strong else SignalSeverity.MEDIUM
                    ),
                    title=(
                        f"{len(cluster)} tickets across {len(accounts)} accounts "
                        f"describe {'the same problem' if strong else 'a possibly related problem'}"
                    ),
                    detail=(
                        f"{', '.join(ids)} share the terms "
                        f"{', '.join(shared[:8])}. Affected accounts: {who}. "
                        f"A problem reported by more than one customer is an "
                        f"operations concern rather than an isolated report."
                    ),
                    affected_account_ids=accounts,
                    record_refs=[_ticket_ref(t) for t in cluster],
                    first_observed_at=min(observed) if observed else None,
                    last_observed_at=max(observed) if observed else None,
                    evidence_chunk_ids=known_issue_chunks,
                    trust_status=(
                        TrustStatus.CONFIDENT.value
                        if strong
                        else TrustStatus.CONDITIONAL.value
                    ),
                    trust_reasons=[] if strong else [weak_reason],
                    recommended_next_step=(
                        "Check whether this matches a documented known issue, and "
                        "consider an operations-level response rather than "
                        "answering each ticket separately."
                        if strong
                        else "Confirm these describe the same problem before "
                        "treating this as an operations-level issue."
                    ),
                )
            )
        else:
            account = accounts[0]
            signals.append(
                Signal(
                    signal_id=_signal_id("RECUR", *ids),
                    signal_type=SignalType.RECURRING_ISSUE,
                    severity=(
                        SignalSeverity.MEDIUM if strong else SignalSeverity.LOW
                    ),
                    title=(
                        f"{names.get(account, account)} may have reported the same "
                        f"problem {len(cluster)} times"
                        if not strong
                        else f"{names.get(account, account)} has reported the same "
                        f"problem {len(cluster)} times"
                    ),
                    detail=(
                        f"{', '.join(ids)} share the terms "
                        f"{', '.join(shared[:8])}. A repeat report suggests the "
                        f"first response did not resolve the underlying cause."
                    ),
                    affected_account_ids=accounts,
                    record_refs=[_ticket_ref(t) for t in cluster],
                    first_observed_at=min(observed) if observed else None,
                    last_observed_at=max(observed) if observed else None,
                    evidence_chunk_ids=known_issue_chunks,
                    trust_status=(
                        TrustStatus.CONFIDENT.value
                        if strong
                        else TrustStatus.CONDITIONAL.value
                    ),
                    trust_reasons=[] if strong else [weak_reason],
                    recommended_next_step=(
                        "Review what was done on the earlier ticket before "
                        "responding again."
                        if strong
                        else "Read both tickets to confirm they describe the same "
                        "problem."
                    ),
                )
            )
    return signals


def detect_operational_anomalies(
    orders: list[Order],
    names: dict[str, str],
    *,
    reference_time: datetime | None,
) -> list[Signal]:
    """Measurable deviations in order activity.

    Two rules, both arithmetic over recorded timestamps rather than inference:

    - **Overdue pickup.** The pickup window closed and no pickup was recorded.
      Grouped by carrier, because a carrier missing windows for several
      accounts is one operations problem, not several support tickets.
    - **Cancellation concentration.** More than half the orders in scope carry
      a cancellation request. Reported as an observation with its own
      denominator, never as a cause.
    """
    signals: list[Signal] = []

    # --- overdue pickups, grouped by carrier ---
    if reference_time is not None:
        overdue: dict[str, list[Order]] = {}
        for order in orders:
            if order.pickup_actual_at is not None or order.pickup_window_end is None:
                continue
            if order.pickup_window_end < reference_time:
                overdue.setdefault(order.carrier or "(unknown carrier)", []).append(order)

        for carrier, late in sorted(overdue.items()):
            accounts = sorted({o.account_id for o in late})
            worst = min(o.pickup_window_end for o in late if o.pickup_window_end)
            minutes = int((reference_time - worst).total_seconds() // 60)
            cross = len(accounts) >= CROSS_CUSTOMER_MIN_ACCOUNTS
            faults = sum(1 for o in late if o.carrier_fault)

            signals.append(
                Signal(
                    signal_id=_signal_id(
                        "PICKUP", carrier.replace(" ", "_"), *(o.order_id for o in late)
                    ),
                    signal_type=(
                        SignalType.CROSS_CUSTOMER_ISSUE
                        if cross
                        else SignalType.OPERATIONAL_ANOMALY
                    ),
                    severity=SignalSeverity.HIGH if cross else SignalSeverity.MEDIUM,
                    title=(
                        f"{carrier}: {len(late)} pickup window(s) closed with no "
                        f"pickup recorded"
                    ),
                    detail=(
                        f"{', '.join(sorted(o.order_id for o in late))} have passed "
                        f"their pickup window with no actual pickup recorded. The "
                        f"oldest is {minutes} minutes past its window, measured "
                        f"against the dataset snapshot. Accounts affected: "
                        f"{', '.join(names.get(a, a) for a in accounts)}. "
                        f"Orders flagged carrier-fault: {faults}."
                    ),
                    affected_account_ids=accounts,
                    record_refs=[_order_ref(o) for o in late],
                    first_observed_at=worst,
                    last_observed_at=reference_time,
                    trust_status=TrustStatus.CONFIDENT.value,
                    recommended_next_step=(
                        "Confirm with the carrier whether pickup occurred but was "
                        "not reported, before treating this as a missed pickup."
                    ),
                )
            )

    # --- cancellation concentration ---
    cancelled = [o for o in orders if o.cancellation_requested_at is not None]
    if orders and len(cancelled) * 2 > len(orders):
        accounts = sorted({o.account_id for o in cancelled})
        observed = [o.cancellation_requested_at for o in cancelled if o.cancellation_requested_at]
        signals.append(
            Signal(
                signal_id=_signal_id("CANCEL", *(o.order_id for o in cancelled)),
                signal_type=SignalType.OPERATIONAL_ANOMALY,
                severity=SignalSeverity.MEDIUM,
                title=(
                    f"{len(cancelled)} of {len(orders)} orders in scope carry a "
                    f"cancellation request"
                ),
                detail=(
                    f"Cancellation requested on "
                    f"{', '.join(sorted(o.order_id for o in cancelled))} — "
                    f"{len(cancelled)} of {len(orders)} orders visible in this "
                    f"workspace, across {len(accounts)} account(s). This is an "
                    f"observation about volume; it does not establish a cause."
                ),
                affected_account_ids=accounts,
                record_refs=[_order_ref(o) for o in cancelled],
                first_observed_at=min(observed) if observed else None,
                last_observed_at=max(observed) if observed else None,
                trust_status=TrustStatus.CONDITIONAL.value,
                trust_reasons=[
                    "A concentration of cancellations is a count, not a cause. "
                    "Nothing in the data explains why these were requested."
                ],
                recommended_next_step=(
                    "Check whether these cancellations share a carrier, a cause, "
                    "or a time window before drawing a conclusion."
                ),
            )
        )

    return signals


# --- entry point ------------------------------------------------------------


def detect_signals(
    conn: sqlite3.Connection,
    *,
    allowed_account_ids: Collection[str] | None = None,
) -> tuple[list[Signal], datetime | None]:
    """Run every detector under one tenant scope. Returns (signals, reference time).

    Scope is applied at the *query* layer, so an out-of-scope record is never
    loaded and no detector can see across a tenant boundary even by accident.
    Ranking is deliberately not applied here — see `operations/ranking.py`.
    """
    try:
        reference_time = load_evaluation_context(conn).reference_time
    except PolicyDataError:
        reference_time = None

    tickets = ops.list_tickets(conn, allowed_account_ids=allowed_account_ids)
    open_tickets = [
        t for t in tickets if (t.status or "").strip().lower() == "open"
    ]
    orders = ops.list_orders(conn, allowed_account_ids=allowed_account_ids)
    names = ops.account_names(conn, allowed_account_ids=allowed_account_ids)

    signals: list[Signal] = []
    signals.extend(
        detect_sla_risk(conn, open_tickets, allowed_account_ids=allowed_account_ids)
    )
    # Clustering reads *all* tickets, open and closed: a closed ticket from last
    # week describing the same problem is exactly the evidence that makes a new
    # one a recurrence rather than a first report.
    signals.extend(
        detect_recurring_and_cross_customer(
            conn, tickets, names, allowed_account_ids=allowed_account_ids
        )
    )
    signals.extend(
        detect_operational_anomalies(orders, names, reference_time=reference_time)
    )
    return signals, reference_time
