"""Extract policy parameters from document evidence, deterministically.

This is what makes customer-agreement overrides work *generically*. No rule
here names a customer, an account id, or a file. Instead, each parameter the
policy engine needs — a free-cancellation window, a fee, a delay threshold, a
credit amount, a cap — is recovered from the text of whichever documents the
Phase 3 authority layer says apply to this account, and every recovered value
carries the chunk that stated it.

**Layering.** Evidence is applied weakest-authority-first, so a stronger
source overwrites a weaker one field by field. That reproduces how the
documents themselves describe their relationship: the SOP supplies defaults
("A signed customer agreement may replace the default delay threshold, credit
amount, or cap"), and an agreement overrides only the fields it actually
states. An agreement that sets a cap and says the SOP otherwise applies
therefore keeps the SOP's threshold and amount, with no special-casing.

**Silence is not zero.** A parameter no document states stays `None`, and the
calculators treat an incomplete term set as grounds for verification rather
than substituting a default. Inventing a number here would be inventing an
invoice line.

The patterns are written against the wording the supplied corpus actually
uses and are unit-tested against it. They are intentionally narrow: a clause
this module cannot parse yields `None`, which surfaces as uncertainty
downstream — never a silent wrong figure.
"""

from __future__ import annotations

import re
from decimal import Decimal

from app.backend.models.documents import Evidence
from app.backend.models.policy import (
    CancellationTerms,
    PickupConfirmationLag,
    ResponseTarget,
    ResponseTargets,
    ServiceCreditTerms,
    Severity,
    TermSource,
)

# --- shared fragments ---------------------------------------------------------

_AMOUNT = r"(?:INR|Rs\.?)\s*([\d,]+(?:\.\d+)?)"

# --- cancellation -------------------------------------------------------------

# "No fee within 30 minutes of booking."
_FREE_WINDOW = re.compile(r"no fee within\s+(\d+)\s*minutes?", re.IGNORECASE)

# "After 30 minutes, charge INR 250 ..."
_CANCEL_FEE = re.compile(rf"charge\s+{_AMOUNT}", re.IGNORECASE)

# An affirmative waiver, e.g. "may cancel any BOOKED shipment before pickup
# with no cancellation fee".
_CANCEL_WAIVER = re.compile(
    r"\b(?:with|at)\s+no\s+cancellation\s+fee\b"
    r"|\bno\s+cancellation\s+fee\s+(?:applies|will apply|is charged)\b",
    re.IGNORECASE,
)

# Guards against two real traps in the supplied corpus that a naive
# "no ... cancellation fee" match would read as a waiver:
#   - an agreement stating "No special cancellation-fee waiver applies."
#   - the SOP's own conditional "...unless a customer agreement explicitly
#     waives the cancellation fee", which describes the possibility of a
#     waiver rather than granting one.
_CANCEL_WAIVER_NEGATED = re.compile(
    r"\bno special\b[^.]{0,60}\bwaiver\b" r"|\bunless\b[^.]{0,80}\bwaives\b",
    re.IGNORECASE,
)

# --- service credit ------------------------------------------------------------

# "more than 2 hours past the end of the scheduled pickup window"
_DELAY_THRESHOLD = re.compile(
    r"more than\s+(\d+(?:\.\d+)?)\s*hours?\s+past", re.IGNORECASE
)

# "a fixed INR 300 service credit"
_FIXED_CREDIT = re.compile(rf"fixed\s+{_AMOUNT}", re.IGNORECASE)

# "the lower of INR 500 or 10% of the shipment fee"
_LOWER_OF = re.compile(
    rf"lower of\s+{_AMOUNT}\s+or\s+(\d+(?:\.\d+)?)\s*%", re.IGNORECASE
)

# "Monthly aggregate service credits are capped at INR 5,000."
_MONTHLY_CAP = re.compile(rf"capped at\s+{_AMOUNT}", re.IGNORECASE)

# "Any individual credit above INR 1,000 requires manager approval."
_APPROVAL_THRESHOLD = re.compile(
    rf"above\s+{_AMOUNT}\s+requires\s+manager\s+approval", re.IGNORECASE
)

# --- documented pickup-confirmation lag -----------------------------------------

# "SwiftShip pickup confirmation webhooks can arrive up to 20 minutes late."
#
# A known issue of this shape is the documented reason a shipment can read
# BOOKED after it was physically collected. It is scoped: it names a carrier
# and a bounded delay, and it explains nothing outside either bound.
_PICKUP_CONFIRMATION_LAG = re.compile(
    r"pickup confirmation[^.]{0,80}?up to\s+(\d+)\s*minutes?\s+late", re.IGNORECASE
)


def _to_decimal(raw: str) -> Decimal:
    return Decimal(raw.replace(",", ""))


def _source(field: str, value: object, match: re.Match, item: Evidence) -> TermSource:
    return TermSource(
        field=field,
        value=str(value),
        matched_text=match.group(0).strip(),
        chunk_id=item.chunk_id,
        source_file=item.source_file,
        section_path=item.section_path,
        authority_tier=item.authority_tier,
    )


def _weakest_first(evidence: list[Evidence]) -> list[Evidence]:
    """Weaker authority first, so stronger sources overwrite as we go.

    Tie-broken on chunk_id so the layering order — and therefore every
    extracted value — is identical on every run.
    """
    return sorted(evidence, key=lambda e: (-int(e.authority_tier), e.chunk_id))


def extract_cancellation_terms(evidence: list[Evidence]) -> CancellationTerms:
    """Assemble cancellation parameters from the evidence that applies.

    Callers must pass evidence already scoped to one account (see
    `app/backend/services/documents.py:get_evidence_by_topic`), so another
    customer's agreement can never contribute a term.
    """
    free_window: int | None = None
    fee_amount: Decimal | None = None
    waived = False
    sources: list[TermSource] = []

    for item in _weakest_first(evidence):
        text = item.text

        if match := _FREE_WINDOW.search(text):
            free_window = int(match.group(1))
            sources.append(_source("free_window_minutes", free_window, match, item))

        if match := _CANCEL_FEE.search(text):
            fee_amount = _to_decimal(match.group(1))
            sources.append(_source("fee_amount", fee_amount, match, item))

        # A waiver is only ever granted by a customer agreement. The SOP
        # merely refers to the possibility of one, so restricting the search
        # to agreement-scoped text is the first line of defence; the negation
        # guard is the second.
        if item.account_id is not None and not _CANCEL_WAIVER_NEGATED.search(text):
            if match := _CANCEL_WAIVER.search(text):
                waived = True
                sources.append(_source("fee_waived", True, match, item))

    return CancellationTerms(
        free_window_minutes=free_window,
        fee_amount=fee_amount,
        fee_waived=waived,
        sources=sources,
    )


# --- first-response targets -----------------------------------------------------

# "● P1: 15 minutes, 24x7" / "P2: 1 hour" / "P3: 8 business hours" — the form a
# customer agreement uses, where each severity states its own target inline.
_INLINE_TARGET = re.compile(
    r"\bP([123])\s*[:\-–]\s*([^\n●•]+)", re.IGNORECASE
)

# "30 minutes, 24x7" -> 30 ; "2 hours" -> 120 ; "1 business day" -> no clock value.
_CLOCK_MINUTES = re.compile(r"^(\d+(?:\.\d+)?)\s*minutes?\b", re.IGNORECASE)
_CLOCK_HOURS = re.compile(r"^(\d+(?:\.\d+)?)\s*hours?\b", re.IGNORECASE)

# A target measured in business time. The corpus never defines a business
# calendar, so these are reported but never converted.
_BUSINESS_TIME = re.compile(r"\bbusiness\s+(?:hours?|days?)\b", re.IGNORECASE)

# "P1 incidents should be escalated immediately."
_P1_IMMEDIATE = re.compile(
    r"P1[^.]{0,60}escalated\s+immediately", re.IGNORECASE
)


def _parse_target_text(raw: str) -> tuple[str, int | None, bool]:
    """Normalise one stated target into (text, clock minutes, is business time).

    Returns `minutes = None` whenever the value is not expressed in plain clock
    time, which is the signal the SLA calculator uses to refuse a breach
    verdict rather than invent a business calendar.
    """
    text = " ".join(raw.split()).strip(" .;,")
    if _BUSINESS_TIME.search(text):
        return text, None, True
    if match := _CLOCK_MINUTES.match(text):
        return text, int(Decimal(match.group(1))), False
    if match := _CLOCK_HOURS.match(text):
        return text, int(Decimal(match.group(1)) * 60), False
    return text, None, False


def _plan_targets_from_table(text: str, plan: str) -> dict[str, str]:
    """Recover one plan's row from the policy's flattened target table.

    PDF extraction renders the table as a column header block followed by one
    label-then-values run per plan:

        Plan / P1 / P2 / P3 / Enterprise / 30 minutes, 24x7 / 2 hours / ...

    So the three lines following a plan's own label are its targets, in the
    severity order the header declared. Anything that does not match that shape
    yields nothing rather than a guess.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lowered = [line.lower() for line in lines]

    try:
        header = lowered.index("plan")
    except ValueError:
        return {}

    severities = [line.upper() for line in lines[header + 1 : header + 4]]
    if severities != ["P1", "P2", "P3"]:
        return {}

    for index, line in enumerate(lowered):
        if index <= header or line != plan.strip().lower():
            continue
        values = lines[index + 1 : index + 4]
        if len(values) == 3:
            return dict(zip(severities, values))
    return {}


def extract_response_targets(
    evidence: list[Evidence], *, plan: str | None = None
) -> ResponseTargets:
    """Assemble first-response targets from the evidence that applies.

    Layered weakest-authority-first exactly as the other extractors are, so a
    customer agreement's inline targets overwrite the plan defaults for the
    severities it actually states — and leave the rest in place. Callers must
    pass evidence already scoped to one account.
    """
    targets: dict[Severity, ResponseTarget] = {}
    sources: list[TermSource] = []
    escalate_p1 = False

    for item in _weakest_first(evidence):
        text = item.text

        if _P1_IMMEDIATE.search(text):
            escalate_p1 = True

        # General policy states targets as a per-plan table; an agreement
        # states them inline. A document may legitimately do neither.
        if plan and item.account_id is None:
            for severity_raw, raw_value in _plan_targets_from_table(text, plan).items():
                severity = Severity.parse(severity_raw)
                if severity is None:
                    continue
                value, minutes, business = _parse_target_text(raw_value)
                match = re.search(re.escape(raw_value), text) or re.search(
                    re.escape(severity_raw), text
                )
                source = _source(f"target_{severity.value}", value, match, item) if match else None
                targets[severity] = ResponseTarget(
                    severity=severity,
                    text=value,
                    minutes=minutes,
                    is_business_time=business,
                    source=source,
                )
                if source is not None:
                    sources.append(source)

        for match in _INLINE_TARGET.finditer(text):
            severity = Severity.parse(f"P{match.group(1)}")
            if severity is None:
                continue
            value, minutes, business = _parse_target_text(match.group(2))
            if not value:
                continue
            source = _source(f"target_{severity.value}", value, match, item)
            targets[severity] = ResponseTarget(
                severity=severity,
                text=value,
                minutes=minutes,
                is_business_time=business,
                source=source,
            )
            sources.append(source)

    return ResponseTargets(
        targets=targets,
        escalate_p1_immediately=escalate_p1,
        plan=plan,
        sources=sources,
    )


# --- severity stated in a request ------------------------------------------------

# "it is a P1", "treat as P2", "P3 question". Matches a severity the *caller*
# asserts, which is a fact about the request rather than a judgement about the
# ticket — see the note on classification in policies/sla.py.
_STATED_SEVERITY = re.compile(r"\bP([123])\b")


def severity_stated_in(message: str) -> Severity | None:
    """The severity a request explicitly names, if exactly one is named.

    Deliberately not a classifier. It reads a label the caller supplied; it
    does not decide what severity a ticket deserves. Two different severities
    in one message is an ambiguity to surface, not to resolve, so it yields
    `None`.
    """
    found = {f"P{match.group(1)}" for match in _STATED_SEVERITY.finditer(message or "")}
    if len(found) != 1:
        return None
    return Severity.parse(next(iter(found)))


def extract_pickup_confirmation_lag(
    evidence: list[Evidence], carrier: str | None
) -> PickupConfirmationLag | None:
    """The documented pickup-confirmation lag for `carrier`, if one exists.

    The carrier is matched by taking the value recorded on the *order* and
    looking for it in the known-issue text, rather than by extracting carrier
    names from prose. That keeps the match generic — no carrier is named in
    this module — while still refusing to apply one carrier's documented
    webhook delay to a different carrier's shipment.

    Returns `None` when no in-force document describes such a lag for this
    carrier, which is the honest answer: absent a documented lag there is no
    evidence-backed reason to doubt an unconfirmed pickup on timing grounds.
    """
    if not carrier or not carrier.strip():
        return None
    needle = carrier.strip().lower()

    for item in _weakest_first(evidence):
        text = item.text
        if needle not in text.lower():
            continue
        if match := _PICKUP_CONFIRMATION_LAG.search(text):
            return PickupConfirmationLag(
                carrier=carrier.strip(),
                lag_minutes=int(match.group(1)),
                source=_source("pickup_confirmation_lag_minutes", match.group(1), match, item),
            )
    return None


def extract_service_credit_terms(evidence: list[Evidence]) -> ServiceCreditTerms:
    """Assemble failed-pickup service-credit parameters from the evidence
    that applies, scoped to one account by the caller."""
    threshold: Decimal | None = None
    fixed: Decimal | None = None
    percentage: Decimal | None = None
    maximum: Decimal | None = None
    monthly_cap: Decimal | None = None
    approval_above: Decimal | None = None
    sources: list[TermSource] = []

    for item in _weakest_first(evidence):
        text = item.text

        if match := _DELAY_THRESHOLD.search(text):
            threshold = _to_decimal(match.group(1))
            sources.append(_source("delay_threshold_hours", threshold, match, item))

        if match := _LOWER_OF.search(text):
            maximum = _to_decimal(match.group(1))
            percentage = _to_decimal(match.group(2))
            sources.append(_source("max_amount", maximum, match, item))
            sources.append(_source("percentage_of_fee", percentage, match, item))

        if match := _FIXED_CREDIT.search(text):
            fixed = _to_decimal(match.group(1))
            sources.append(_source("fixed_amount", fixed, match, item))

        if match := _MONTHLY_CAP.search(text):
            monthly_cap = _to_decimal(match.group(1))
            sources.append(_source("monthly_cap", monthly_cap, match, item))

        if match := _APPROVAL_THRESHOLD.search(text):
            approval_above = _to_decimal(match.group(1))
            sources.append(_source("manager_approval_above", approval_above, match, item))

    return ServiceCreditTerms(
        delay_threshold_hours=threshold,
        fixed_amount=fixed,
        percentage_of_fee=percentage,
        max_amount=maximum,
        monthly_cap=monthly_cap,
        manager_approval_above=approval_above,
        sources=sources,
    )
