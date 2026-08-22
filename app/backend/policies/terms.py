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
from app.backend.models.policy import CancellationTerms, ServiceCreditTerms, TermSource

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
