"""Source authority: classification and deterministic conflict resolution.

Two responsibilities, both pure functions over data — no database, no PDF
parsing, no LLM:

1. **Classification.** Turn facts stated in a document (its `Status:` line,
   its title, whether it names an `Account:`) into a `DocumentType`, a
   `DocumentStatus`, and an `AuthorityTier`; and turn a section heading into
   a `Topic`.

2. **Resolution.** Given a set of `Evidence`, decide which items *govern* and
   which are merely *context*, emitting an explicit note for every override
   and every conflict precedence cannot settle.

The central rule this module exists to enforce: **retrieval relevance never
decides which source wins.** A deprecated policy can be the best textual
match for a query and still must never govern. Authority is computed from
stated document metadata, in code, before any model sees the evidence.
"""

from __future__ import annotations

from collections import defaultdict

from app.backend.models.documents import (
    AuthorityDecision,
    AuthorityTier,
    ConflictNote,
    DocumentStatus,
    DocumentType,
    Evidence,
    OverrideNote,
    Topic,
)


class UnknownAuthorityError(ValueError):
    """A document states something we cannot map to a status or type. Raised
    rather than guessed: silently defaulting an unrecognised status would
    decide authority on a coin flip."""


# Statuses that mean "this document is in force".
_CURRENT_STATUSES = frozenset({DocumentStatus.CURRENT, DocumentStatus.ACTIVE})


def normalise_status(raw: str) -> DocumentStatus:
    """Map a document's stated `Status:` value to a DocumentStatus.

    Real values in the supplied pack include "CURRENT", "ACTIVE", and
    "DEPRECATED - DO NOT USE FOR CURRENT REQUESTS"; only the leading keyword
    is significant, the remainder is emphasis.
    """
    if not raw or not raw.strip():
        raise UnknownAuthorityError("document states no Status")
    keyword = raw.strip().split()[0].strip(":-,.").upper()
    try:
        return DocumentStatus(keyword)
    except ValueError as exc:
        raise UnknownAuthorityError(
            f"unrecognised document status {raw!r} (leading keyword {keyword!r}); "
            f"expected one of {sorted(s.value for s in DocumentStatus)}"
        ) from exc


def classify_document_type(*, title: str, has_account: bool) -> DocumentType:
    """Derive the document type from what the document itself says.

    A document that names an `Account:` in its preamble is a customer
    agreement; otherwise the title decides. Deliberately keyword-based on the
    real titles rather than filename-based, so a renamed file cannot silently
    change a document's authority.
    """
    if has_account:
        return DocumentType.CUSTOMER_AGREEMENT

    lowered = title.lower()
    if "support policy" in lowered:
        return DocumentType.SUPPORT_POLICY
    if "sop" in lowered:
        return DocumentType.SOP
    if "operations guide" in lowered or "product" in lowered:
        return DocumentType.PRODUCT_DOCUMENTATION
    raise UnknownAuthorityError(
        f"cannot determine document type from title {title!r} and no Account: field is present"
    )


def authority_tier_for(document_type: DocumentType, status: DocumentStatus) -> AuthorityTier:
    """Compute the precedence tier.

    Status dominates type: a deprecated support policy is non-authoritative,
    not a tier-2 source. This is what makes
    02_Support_Policy_v2_DEPRECATED.pdf structurally incapable of governing,
    regardless of how well it matches a query.
    """
    if status not in _CURRENT_STATUSES:
        return AuthorityTier.NON_AUTHORITATIVE
    if document_type is DocumentType.CUSTOMER_AGREEMENT:
        return AuthorityTier.CUSTOMER_AGREEMENT
    if document_type is DocumentType.SUPPORT_POLICY:
        return AuthorityTier.CURRENT_SUPPORT_POLICY
    return AuthorityTier.CURRENT_OPERATIONAL_DOC


def is_authoritative(status: DocumentStatus) -> bool:
    return status in _CURRENT_STATUSES


# Section-heading keyword → topic. Ordered: the first entry whose keyword
# appears in the heading wins, so more specific phrases must precede more
# general ones ("service credit" before "cancellation" is irrelevant here,
# but "credit" must precede nothing that would swallow it).
_TOPIC_KEYWORDS: tuple[tuple[str, Topic], ...] = (
    ("credit", Topic.SERVICE_CREDIT),
    ("cancel", Topic.CANCELLATION),
    ("support term", Topic.SUPPORT_RESPONSE),
    ("severity", Topic.SUPPORT_RESPONSE),
    ("response", Topic.SUPPORT_RESPONSE),
    ("escalation", Topic.SUPPORT_RESPONSE),
    ("known issue", Topic.PRODUCT_KNOWN_ISSUES),
    ("resolved issue", Topic.PRODUCT_KNOWN_ISSUES),
    ("capabilit", Topic.PRODUCT_KNOWN_ISSUES),
)


def classify_topic(section_title: str | None, parent_section_title: str | None = None) -> Topic:
    """Derive a chunk's subject-matter topic from its section heading.

    A subsection inherits its parent's topic when its own heading carries no
    keyword — e.g. "KI-208 - Bulk Upload failures on large CSVs" under
    "2. Current known issues".

    Preamble chunks (no heading) are GENERAL: a document header states what
    the document *is*, not what it rules on.
    """
    for candidate in (section_title, parent_section_title):
        if not candidate:
            continue
        lowered = candidate.lower()
        for keyword, topic in _TOPIC_KEYWORDS:
            if keyword in lowered:
                return topic
    return Topic.GENERAL


def _may_govern(item: Evidence, account_id: str | None) -> bool:
    """Whether a piece of evidence is *eligible* to govern this resolution.

    Two gates, both independent of how well it matched the query:

    1. It must be in force. Deprecated/superseded material never governs.
    2. A customer agreement may only govern a resolution scoped to *its own*
       account. An agreement is customer-scoped authority, not general
       authority — letting Northstar's terms outrank the standard support
       policy on an unscoped question would be exactly the cross-customer
       bleed docs/architecture.md calls a correctness bug.

    Consequence worth stating plainly: on an unscoped search every customer
    agreement is demoted to context. It stays retrievable and fully readable
    (answering "which customers have custom cancellation terms?" needs it),
    it simply cannot decide the answer. To get agreement precedence, a caller
    must say which account it is asking about.
    """
    if not item.is_authoritative:
        return False
    if item.document_type is DocumentType.CUSTOMER_AGREEMENT:
        return account_id is not None and item.account_id == account_id
    return True


def resolve_authority(
    evidence: list[Evidence], *, account_id: str | None = None
) -> AuthorityDecision:
    """Split evidence into governing vs. contextual, deterministically.

    Per topic:

    - Evidence that is not eligible to govern (see `_may_govern`) is always
      contextual, but never dropped — quoting a superseded rule is how you
      explain that a rule changed, and reading another customer's terms is
      sometimes the question itself.
    - Among eligible evidence, the best (numerically lowest) tier present
      governs; everything at a weaker tier becomes contextual and earns an
      `OverrideNote` naming both sides.
    - If two *different documents* share the governing tier for a topic,
      precedence cannot settle it: both are returned as governing and a
      `ConflictNote` is emitted for escalation. Picking one arbitrarily is
      exactly the failure mode this layer exists to prevent.

    Topics are resolved independently, which is what "current SOP / product
    documentation according to the subject matter" means in practice: the
    cancellation SOP governing cancellations does not make it govern severity
    definitions. Evidence on several topics therefore yields governing items
    on several topics; use `AuthorityDecision.governing_for` to read one.

    `account_id` is the account the question is *about*. It is a precedence
    input only — it performs no access control. Restricting which documents a
    caller may see at all happens earlier, in SQL, in
    app/backend/services/documents.py.

    Ordering is stable (tier, then chunk_id) so identical input always yields
    identical output.
    """
    by_topic: dict[Topic, list[Evidence]] = defaultdict(list)
    for item in evidence:
        by_topic[item.topic].append(item)

    governing: list[Evidence] = []
    contextual: list[Evidence] = []
    overrides: list[OverrideNote] = []
    conflicts: list[ConflictNote] = []

    for topic in sorted(by_topic, key=lambda t: t.value):
        items = by_topic[topic]
        eligible = [e for e in items if _may_govern(e, account_id)]
        contextual.extend([e for e in items if not _may_govern(e, account_id)])

        if not eligible:
            # Nothing in force and in scope on this topic; retrieved material
            # stays context-only and nothing governs.
            continue

        best_tier = min(e.authority_tier for e in eligible)
        winners = [e for e in eligible if e.authority_tier == best_tier]
        losers = [e for e in eligible if e.authority_tier != best_tier]

        governing.extend(winners)
        contextual.extend(losers)

        for loser in losers:
            for winner in winners:
                overrides.append(
                    OverrideNote(
                        topic=topic,
                        winning_chunk_id=winner.chunk_id,
                        winning_document_id=winner.document_id,
                        winning_source_file=winner.source_file,
                        winning_authority_tier=winner.authority_tier,
                        overridden_chunk_id=loser.chunk_id,
                        overridden_document_id=loser.document_id,
                        overridden_source_file=loser.source_file,
                        overridden_authority_tier=loser.authority_tier,
                        reason=(
                            f"{winner.citation} (tier {int(winner.authority_tier)} "
                            f"{winner.authority_tier.name}) outranks "
                            f"{loser.citation} (tier {int(loser.authority_tier)} "
                            f"{loser.authority_tier.name}) on topic '{topic.value}'"
                        ),
                    )
                )

        winning_documents = sorted({e.document_id for e in winners})
        if len(winning_documents) > 1:
            conflicts.append(
                ConflictNote(
                    topic=topic,
                    authority_tier=best_tier,
                    chunk_ids=tuple(sorted(e.chunk_id for e in winners)),
                    document_ids=tuple(winning_documents),
                    reason=(
                        f"{len(winning_documents)} different documents share authority tier "
                        f"{int(best_tier)} ({best_tier.name}) on topic '{topic.value}'; "
                        f"precedence cannot resolve this — escalate"
                    ),
                )
            )

    def sort_key(e: Evidence) -> tuple[int, str]:
        return (int(e.authority_tier), e.chunk_id)

    return AuthorityDecision(
        governing=sorted(governing, key=sort_key),
        contextual=sorted(contextual, key=sort_key),
        overrides=overrides,
        conflicts=conflicts,
    )
