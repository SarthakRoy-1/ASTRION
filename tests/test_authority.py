"""Phase 3: source authority classification and conflict resolution.

Unit tests over synthetic Evidence, so precedence logic is exercised in
isolation from PDF parsing, the database, and retrieval scoring. The
end-to-end versions of the same rules — against the real documents — live in
tests/test_search.py.
"""

import pytest

from app.backend.models.documents import (
    AuthorityTier,
    DocumentStatus,
    DocumentType,
    Evidence,
    Topic,
)
from app.backend.retrieval.authority import (
    UnknownAuthorityError,
    authority_tier_for,
    classify_document_type,
    classify_topic,
    is_authoritative,
    normalise_status,
    resolve_authority,
)


def make_evidence(
    chunk_id: str,
    *,
    document_id: str | None = None,
    document_type: DocumentType = DocumentType.SOP,
    status: DocumentStatus = DocumentStatus.CURRENT,
    topic: Topic = Topic.CANCELLATION,
    account_id: str | None = None,
    authority_tier: AuthorityTier | None = None,
    text: str = "some clause text",
) -> Evidence:
    document_id = document_id or f"doc_{chunk_id}"
    tier = authority_tier or authority_tier_for(document_type, status)
    return Evidence(
        chunk_id=chunk_id,
        document_id=document_id,
        text=text,
        source_file=f"{document_id}.pdf",
        source_sha256="0" * 64,
        page_number=1,
        section_number="1",
        section_title="A section",
        subsection_title=None,
        section_path="1. A section",
        page_char_start=0,
        page_char_end=len(text),
        document_title=document_id,
        document_type=document_type,
        status=status,
        status_raw=status.value,
        is_current=is_authoritative(status),
        is_deprecated=status is DocumentStatus.DEPRECATED,
        is_authoritative=is_authoritative(status),
        authority_tier=tier,
        topic=topic,
        effective_date=None,
        updated_date=None,
        supersedes=None,
        superseded_by=None,
        account_id=account_id,
        customer_name=None,
    )


# --- status normalisation ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CURRENT", DocumentStatus.CURRENT),
        ("ACTIVE", DocumentStatus.ACTIVE),
        ("DEPRECATED", DocumentStatus.DEPRECATED),
        ("DEPRECATED - DO NOT USE FOR CURRENT REQUESTS", DocumentStatus.DEPRECATED),
        ("  current  ", DocumentStatus.CURRENT),
    ],
)
def test_normalise_status(raw, expected):
    assert normalise_status(raw) is expected


@pytest.mark.parametrize("raw", ["", "   ", "Investigating", "Banana"])
def test_normalise_status_rejects_unknown(raw):
    with pytest.raises(UnknownAuthorityError):
        normalise_status(raw)


# --- document typing ------------------------------------------------------------


def test_account_bearing_document_is_a_customer_agreement():
    assert (
        classify_document_type(title="Anything At All", has_account=True)
        is DocumentType.CUSTOMER_AGREEMENT
    )


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("ParcelPilot Support Policy v3", DocumentType.SUPPORT_POLICY),
        ("ParcelPilot Cancellation & Service Credit SOP v4", DocumentType.SOP),
        ("ParcelPilot Product Operations Guide", DocumentType.PRODUCT_DOCUMENTATION),
    ],
)
def test_classify_document_type_from_title(title, expected):
    assert classify_document_type(title=title, has_account=False) is expected


def test_unclassifiable_title_raises():
    with pytest.raises(UnknownAuthorityError):
        classify_document_type(title="Company Picnic Flyer", has_account=False)


# --- tiers -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("doc_type", "status", "expected"),
    [
        (DocumentType.CUSTOMER_AGREEMENT, DocumentStatus.ACTIVE, AuthorityTier.CUSTOMER_AGREEMENT),
        (DocumentType.SUPPORT_POLICY, DocumentStatus.CURRENT, AuthorityTier.CURRENT_SUPPORT_POLICY),
        (DocumentType.SOP, DocumentStatus.CURRENT, AuthorityTier.CURRENT_OPERATIONAL_DOC),
        (
            DocumentType.PRODUCT_DOCUMENTATION,
            DocumentStatus.CURRENT,
            AuthorityTier.CURRENT_OPERATIONAL_DOC,
        ),
    ],
)
def test_authority_tier_for_current_documents(doc_type, status, expected):
    assert authority_tier_for(doc_type, status) is expected


@pytest.mark.parametrize(
    "doc_type",
    [
        DocumentType.CUSTOMER_AGREEMENT,
        DocumentType.SUPPORT_POLICY,
        DocumentType.SOP,
        DocumentType.PRODUCT_DOCUMENTATION,
    ],
)
def test_deprecated_status_makes_any_document_non_authoritative(doc_type):
    """Status dominates type: a deprecated support policy is not a tier-2
    source, it is structurally incapable of governing."""
    assert (
        authority_tier_for(doc_type, DocumentStatus.DEPRECATED)
        is AuthorityTier.NON_AUTHORITATIVE
    )


def test_tier_ordering_is_lower_is_stronger():
    assert (
        AuthorityTier.CUSTOMER_AGREEMENT
        < AuthorityTier.CURRENT_SUPPORT_POLICY
        < AuthorityTier.CURRENT_OPERATIONAL_DOC
        < AuthorityTier.NON_AUTHORITATIVE
    )


# --- topic classification ----------------------------------------------------------


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("1. Order cancellation", Topic.CANCELLATION),
        ("2. Cancellation terms", Topic.CANCELLATION),
        ("3. Service credits", Topic.SERVICE_CREDIT),
        ("3. Failed-pickup credits", Topic.SERVICE_CREDIT),
        ("1. Support terms", Topic.SUPPORT_RESPONSE),
        ("2. Severity definitions", Topic.SUPPORT_RESPONSE),
        ("4. Escalation", Topic.SUPPORT_RESPONSE),
        ("2. Current known issues", Topic.PRODUCT_KNOWN_ISSUES),
        ("1. Plan capabilities", Topic.PRODUCT_KNOWN_ISSUES),
        ("4. Account contact", Topic.GENERAL),
    ],
)
def test_classify_topic(heading, expected):
    assert classify_topic(heading) is expected


def test_topic_falls_back_to_parent_heading():
    assert (
        classify_topic("KI-208 - Bulk Upload failures", "2. Current known issues")
        is Topic.PRODUCT_KNOWN_ISSUES
    )


def test_preamble_has_no_topic_of_its_own():
    assert classify_topic(None, None) is Topic.GENERAL


# --- resolution: deprecated ---------------------------------------------------------


def test_deprecated_never_governs_even_when_alone():
    deprecated = make_evidence(
        "dep", document_type=DocumentType.SUPPORT_POLICY, status=DocumentStatus.DEPRECATED
    )

    decision = resolve_authority([deprecated])

    assert decision.governing == []
    assert decision.contextual == [deprecated]


def test_deprecated_is_retained_as_context_not_dropped():
    """Explaining that a rule changed requires quoting the superseded rule."""
    current = make_evidence("cur", document_type=DocumentType.SUPPORT_POLICY)
    deprecated = make_evidence(
        "dep", document_type=DocumentType.SUPPORT_POLICY, status=DocumentStatus.DEPRECATED
    )

    decision = resolve_authority([current, deprecated])

    assert decision.governing == [current]
    assert deprecated in decision.contextual


def test_deprecated_loses_even_when_listed_first():
    deprecated = make_evidence(
        "a_dep", document_type=DocumentType.SUPPORT_POLICY, status=DocumentStatus.DEPRECATED
    )
    current = make_evidence("z_cur", document_type=DocumentType.SUPPORT_POLICY)

    decision = resolve_authority([deprecated, current])

    assert decision.governing == [current]


# --- resolution: customer agreement precedence ---------------------------------------


def test_agreement_overrides_general_doc_for_its_own_account():
    sop = make_evidence("sop", document_type=DocumentType.SOP, topic=Topic.CANCELLATION)
    agreement = make_evidence(
        "agr",
        document_type=DocumentType.CUSTOMER_AGREEMENT,
        status=DocumentStatus.ACTIVE,
        topic=Topic.CANCELLATION,
        account_id="ACCT-001",
    )

    decision = resolve_authority([sop, agreement], account_id="ACCT-001")

    assert decision.governing == [agreement]
    assert sop in decision.contextual
    assert len(decision.overrides) == 1
    assert decision.overrides[0].winning_chunk_id == "agr"
    assert decision.overrides[0].overridden_chunk_id == "sop"
    assert decision.overrides[0].topic is Topic.CANCELLATION


def test_agreement_does_not_govern_an_unscoped_resolution():
    """A customer agreement is customer-scoped authority. On a question that
    names no account it must not outrank general policy — that is exactly the
    cross-customer bleed the architecture forbids."""
    sop = make_evidence("sop", document_type=DocumentType.SOP, topic=Topic.CANCELLATION)
    agreement = make_evidence(
        "agr",
        document_type=DocumentType.CUSTOMER_AGREEMENT,
        status=DocumentStatus.ACTIVE,
        topic=Topic.CANCELLATION,
        account_id="ACCT-001",
    )

    decision = resolve_authority([sop, agreement], account_id=None)

    assert decision.governing == [sop]
    assert agreement in decision.contextual
    assert decision.overrides == []


def test_agreement_does_not_govern_a_different_accounts_resolution():
    sop = make_evidence("sop", document_type=DocumentType.SOP, topic=Topic.CANCELLATION)
    other_agreement = make_evidence(
        "agr",
        document_type=DocumentType.CUSTOMER_AGREEMENT,
        status=DocumentStatus.ACTIVE,
        topic=Topic.CANCELLATION,
        account_id="ACCT-002",
    )

    decision = resolve_authority([sop, other_agreement], account_id="ACCT-001")

    assert decision.governing == [sop]
    assert other_agreement in decision.contextual


def test_expired_or_inactive_agreement_cannot_govern():
    agreement = make_evidence(
        "agr",
        document_type=DocumentType.CUSTOMER_AGREEMENT,
        status=DocumentStatus.DEPRECATED,
        topic=Topic.CANCELLATION,
        account_id="ACCT-001",
    )
    sop = make_evidence("sop", document_type=DocumentType.SOP, topic=Topic.CANCELLATION)

    decision = resolve_authority([agreement, sop], account_id="ACCT-001")

    assert decision.governing == [sop]


# --- resolution: topics are independent -------------------------------------------------


def test_topics_resolve_independently():
    """An agreement overriding cancellation terms does not thereby govern
    severity definitions."""
    agreement_cancel = make_evidence(
        "agr_cancel",
        document_type=DocumentType.CUSTOMER_AGREEMENT,
        status=DocumentStatus.ACTIVE,
        topic=Topic.CANCELLATION,
        account_id="ACCT-001",
    )
    sop_cancel = make_evidence("sop_cancel", document_type=DocumentType.SOP, topic=Topic.CANCELLATION)
    policy_support = make_evidence(
        "pol_support",
        document_type=DocumentType.SUPPORT_POLICY,
        topic=Topic.SUPPORT_RESPONSE,
    )

    decision = resolve_authority(
        [agreement_cancel, sop_cancel, policy_support], account_id="ACCT-001"
    )

    assert decision.governing_for(Topic.CANCELLATION) == [agreement_cancel]
    assert decision.governing_for(Topic.SUPPORT_RESPONSE) == [policy_support]
    assert sop_cancel in decision.contextual


def test_support_policy_outranks_operational_doc_on_a_shared_topic():
    policy = make_evidence(
        "pol", document_type=DocumentType.SUPPORT_POLICY, topic=Topic.SUPPORT_RESPONSE
    )
    guide = make_evidence(
        "guide",
        document_type=DocumentType.PRODUCT_DOCUMENTATION,
        topic=Topic.SUPPORT_RESPONSE,
    )

    decision = resolve_authority([policy, guide])

    assert decision.governing == [policy]
    assert guide in decision.contextual


# --- resolution: unresolved conflicts -----------------------------------------------------


def test_same_tier_different_documents_is_reported_as_a_conflict():
    """Precedence cannot settle this, so it escalates rather than picking."""
    a = make_evidence("a", document_id="doc_a", document_type=DocumentType.SOP)
    b = make_evidence("b", document_id="doc_b", document_type=DocumentType.SOP)

    decision = resolve_authority([a, b])

    assert decision.has_unresolved_conflict is True
    assert len(decision.conflicts) == 1
    assert decision.conflicts[0].document_ids == ("doc_a", "doc_b")
    # Neither is silently discarded.
    assert set(decision.governing) == {a, b}


def test_same_tier_same_document_is_not_a_conflict():
    a = make_evidence("a", document_id="same_doc", document_type=DocumentType.SOP)
    b = make_evidence("b", document_id="same_doc", document_type=DocumentType.SOP)

    decision = resolve_authority([a, b])

    assert decision.has_unresolved_conflict is False


# --- determinism -----------------------------------------------------------------------------


def test_resolution_is_order_independent_and_stable():
    items = [
        make_evidence("c", document_type=DocumentType.SOP, topic=Topic.CANCELLATION),
        make_evidence(
            "a",
            document_type=DocumentType.CUSTOMER_AGREEMENT,
            status=DocumentStatus.ACTIVE,
            topic=Topic.CANCELLATION,
            account_id="ACCT-001",
        ),
        make_evidence(
            "b",
            document_type=DocumentType.SUPPORT_POLICY,
            status=DocumentStatus.DEPRECATED,
            topic=Topic.CANCELLATION,
        ),
    ]

    first = resolve_authority(items, account_id="ACCT-001")
    second = resolve_authority(list(reversed(items)), account_id="ACCT-001")

    assert [e.chunk_id for e in first.governing] == [e.chunk_id for e in second.governing]
    assert [e.chunk_id for e in first.contextual] == [e.chunk_id for e in second.contextual]


def test_empty_evidence_resolves_to_empty_decision():
    decision = resolve_authority([])

    assert decision.governing == []
    assert decision.contextual == []
    assert decision.overrides == []
    assert decision.has_unresolved_conflict is False
