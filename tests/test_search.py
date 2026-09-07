"""Phase 3: retrieval, evidence provenance, account isolation, and the
conflict scenarios the assessment calls out by name.

These run against the six real PDFs (session-ingested into a temp database),
so they assert on what the supplied documents actually say. They assert that
the right *source* is retrieved and that the right source *governs* — never
that a particular fee or deadline is the answer, which is Phase 4's job.
"""

import pytest

from app.backend.models.documents import AuthorityTier, DocumentType, Topic
from app.backend.retrieval.search import (
    get_document_evidence,
    search_and_resolve,
    search_documents,
    tokenize,
)
from conftest import (
    CURRENT_POLICY_DOC_ID,
    CURRENT_POLICY_PDF,
    DEPRECATED_POLICY_PDF,
    LUMENWORKS_DOC_ID,
    LUMENWORKS_PDF,
    NORTHSTAR_DOC_ID,
    NORTHSTAR_PDF,
    SOP_PDF,
)

NORTHSTAR = "ACCT-001"
LUMENWORKS = "ACCT-002"


def files_of(evidence):
    return {e.source_file for e in evidence}


# --- tokenizer -----------------------------------------------------------------


def test_tokenizer_preserves_hyphenated_identifiers():
    tokens = tokenize("KI-208 affects ACCT-001")

    assert "ki-208" in tokens
    assert "ki" in tokens and "208" in tokens
    assert "acct-001" in tokens


def test_tokenizer_normalises_thousands_separators():
    assert "3000" in tokenize("above approximately 3,000 rows")


def test_tokenizer_keeps_meaning_bearing_short_words():
    tokens = tokenize("no fee may not apply after pickup")

    for word in ("no", "fee", "may", "not", "after", "pickup"):
        assert word in tokens


# --- basic retrieval --------------------------------------------------------------


def test_retrieves_current_support_policy_content(doc_conn):
    results = search_documents(doc_conn, "P1 first response target for Enterprise plan")

    assert results
    assert CURRENT_POLICY_PDF in files_of(results)


def test_retrieves_cancellation_sop_content(doc_conn):
    results = search_documents(doc_conn, "cancel a BOOKED shipment 30 minutes after booking")

    top = results[0]
    assert top.source_file == SOP_PDF
    assert "30 minutes" in top.text


def test_retrieves_known_issue_by_identifier(doc_conn):
    results = search_documents(doc_conn, "KI-208")

    assert results
    assert "KI-208" in results[0].subsection_title


def test_retrieves_northstar_agreement_content(doc_conn):
    results = search_documents(
        doc_conn, "Northstar cancellation of a booked shipment before pickup",
        account_id=NORTHSTAR,
    )

    assert NORTHSTAR_PDF in files_of(results)
    agreement = next(e for e in results if e.source_file == NORTHSTAR_PDF)
    assert agreement.account_id == NORTHSTAR
    assert agreement.customer_name == "Northstar Logistics"


def test_retrieves_lumenworks_agreement_content(doc_conn):
    results = search_documents(
        doc_conn, "failed pickup credit threshold", account_id=LUMENWORKS
    )

    assert LUMENWORKS_PDF in files_of(results)
    agreement = next(e for e in results if e.source_file == LUMENWORKS_PDF)
    assert agreement.account_id == LUMENWORKS


def test_empty_query_returns_nothing(doc_conn):
    assert search_documents(doc_conn, "") == []
    assert search_documents(doc_conn, "   ") == []


def test_query_matching_nothing_returns_nothing(doc_conn):
    assert search_documents(doc_conn, "zzzqqqxyzzy") == []


def test_limit_is_respected(doc_conn):
    results = search_documents(doc_conn, "cancellation credit policy shipment", limit=3)

    assert len(results) <= 3


def test_results_are_ordered_by_descending_score(doc_conn):
    results = search_documents(doc_conn, "service credit carrier fault pickup")

    scores = [e.score for e in results]
    assert scores == sorted(scores, reverse=True)


def test_search_is_deterministic(doc_conn):
    query = "cancellation fee after 30 minutes"
    first = search_documents(doc_conn, query)
    second = search_documents(doc_conn, query)

    assert [(e.chunk_id, e.score) for e in first] == [(e.chunk_id, e.score) for e in second]


# --- evidence provenance -------------------------------------------------------------


def test_evidence_carries_complete_provenance(doc_conn):
    results = search_documents(doc_conn, "failed pickup service credit", limit=5)

    assert results
    for evidence in results:
        assert evidence.chunk_id
        assert evidence.document_id
        assert evidence.text.strip()
        assert evidence.source_file.endswith(".pdf")
        assert len(evidence.source_sha256) == 64
        assert evidence.page_number >= 1
        assert evidence.document_title
        assert isinstance(evidence.document_type, DocumentType)
        assert isinstance(evidence.authority_tier, AuthorityTier)
        assert isinstance(evidence.topic, Topic)
        assert evidence.is_authoritative in (True, False)
        assert evidence.score is not None


def test_evidence_citation_names_file_page_and_section(doc_conn):
    results = search_documents(doc_conn, "cancel a BOOKED shipment before pickup")
    top = next(e for e in results if e.section_path)

    assert top.source_file in top.citation
    assert f"p.{top.page_number}" in top.citation
    assert top.section_path in top.citation


def test_evidence_section_provenance_present_where_document_has_sections(doc_conn):
    results = search_documents(doc_conn, "order cancellation")
    sop = next(e for e in results if e.source_file == SOP_PDF and e.section_number)

    assert sop.section_number == "1"
    assert sop.section_title == "Order cancellation"


# --- authority: deprecated policy ------------------------------------------------------


def test_deprecated_policy_is_retrievable(doc_conn):
    """It must stay findable — explaining that a rule changed needs it."""
    results = search_documents(doc_conn, "Enterprise P1 response target 1 hour", limit=10)

    assert DEPRECATED_POLICY_PDF in files_of(results)


def test_deprecated_policy_is_flagged_non_authoritative(doc_conn):
    results = search_documents(doc_conn, "Enterprise P1 response target 1 hour", limit=10)
    deprecated = next(e for e in results if e.source_file == DEPRECATED_POLICY_PDF)

    assert deprecated.is_deprecated is True
    assert deprecated.is_authoritative is False
    assert deprecated.authority_tier is AuthorityTier.NON_AUTHORITATIVE


def test_deprecated_policy_never_governs(doc_conn):
    decision = search_and_resolve(doc_conn, "Enterprise P1 first response target", limit=10)

    assert DEPRECATED_POLICY_PDF not in files_of(decision.governing)
    assert DEPRECATED_POLICY_PDF in files_of(decision.contextual)


def test_current_policy_governs_support_response(doc_conn):
    decision = search_and_resolve(doc_conn, "Enterprise P1 first response target", limit=10)
    governing = decision.governing_for(Topic.SUPPORT_RESPONSE)

    assert governing
    assert {e.source_file for e in governing} == {CURRENT_POLICY_PDF}


def test_include_non_authoritative_false_excludes_deprecated_entirely(doc_conn):
    results = search_documents(
        doc_conn,
        "Enterprise P1 response target",
        limit=10,
        include_non_authoritative=False,
    )

    assert DEPRECATED_POLICY_PDF not in files_of(results)


def test_deprecated_can_outrank_current_on_relevance_yet_never_on_authority(doc_conn):
    """Guards the separation directly: whatever relevance says, the governing
    set is decided by stated document metadata."""
    results = search_documents(doc_conn, "Superseded by Support Policy v3", limit=10)
    assert DEPRECATED_POLICY_PDF in files_of(results)

    decision = search_and_resolve(doc_conn, "Superseded by Support Policy v3", limit=10)
    assert DEPRECATED_POLICY_PDF not in files_of(decision.governing)


# --- conflict: Northstar cancellation ----------------------------------------------------


def test_northstar_agreement_governs_cancellation_for_northstar(doc_conn):
    decision = search_and_resolve(
        doc_conn,
        "cancellation fee for a BOOKED shipment not yet picked up",
        account_id=NORTHSTAR,
        limit=10,
    )
    governing = decision.governing_for(Topic.CANCELLATION)

    assert [e.source_file for e in governing] == [NORTHSTAR_PDF]
    assert "no cancellation fee" in governing[0].text.lower()


def test_northstar_cancellation_override_names_both_sources(doc_conn):
    decision = search_and_resolve(
        doc_conn,
        "cancellation fee for a BOOKED shipment not yet picked up",
        account_id=NORTHSTAR,
        limit=10,
    )
    override = next(o for o in decision.overrides if o.topic is Topic.CANCELLATION)

    assert override.winning_source_file == NORTHSTAR_PDF
    assert override.overridden_source_file == SOP_PDF
    assert override.winning_authority_tier is AuthorityTier.CUSTOMER_AGREEMENT
    assert "outranks" in override.reason


def test_general_sop_still_governs_cancellation_for_an_account_without_an_agreement(doc_conn):
    """ACCT-003 (Beacon Retail) has no agreement in the pack; the standard SOP
    must govern, and no other customer's terms may appear."""
    decision = search_and_resolve(
        doc_conn,
        "cancellation fee for a BOOKED shipment not yet picked up",
        account_id="ACCT-003",
        limit=10,
    )
    governing = decision.governing_for(Topic.CANCELLATION)

    assert [e.source_file for e in governing] == [SOP_PDF]
    assert NORTHSTAR_PDF not in files_of(decision.governing + decision.contextual)
    assert LUMENWORKS_PDF not in files_of(decision.governing + decision.contextual)


def test_unscoped_cancellation_question_is_governed_by_the_sop(doc_conn):
    """No account named: no customer agreement may decide the answer."""
    decision = search_and_resolve(
        doc_conn, "cancellation fee for a BOOKED shipment not yet picked up", limit=10
    )
    governing = decision.governing_for(Topic.CANCELLATION)

    assert [e.source_file for e in governing] == [SOP_PDF]


# --- conflict: LumenWorks failed-pickup credit ---------------------------------------------


def test_lumenworks_agreement_governs_service_credit_for_lumenworks(doc_conn):
    decision = search_and_resolve(
        doc_conn,
        "failed pickup service credit carrier at fault",
        account_id=LUMENWORKS,
        limit=10,
    )
    governing = decision.governing_for(Topic.SERVICE_CREDIT)

    assert [e.source_file for e in governing] == [LUMENWORKS_PDF]
    assert "INR 300" in governing[0].text


def test_lumenworks_credit_override_supersedes_the_sop_default(doc_conn):
    decision = search_and_resolve(
        doc_conn,
        "failed pickup service credit carrier at fault",
        account_id=LUMENWORKS,
        limit=10,
    )
    override = next(o for o in decision.overrides if o.topic is Topic.SERVICE_CREDIT)

    assert override.winning_source_file == LUMENWORKS_PDF
    assert override.overridden_source_file == SOP_PDF
    # The overridden SOP text stays available in full, so an answer can explain
    # what the default would have been.
    overridden = next(
        e for e in decision.contextual if e.chunk_id == override.overridden_chunk_id
    )
    assert "2 hours" in overridden.text


def test_northstar_has_no_service_credit_amount_override_of_its_own(doc_conn):
    """Northstar's credit clause defers to the SOP except for a monthly cap.
    The agreement still governs the topic for Northstar — the *reasoning* about
    what its text means is the agent's job, not this layer's."""
    decision = search_and_resolve(
        doc_conn, "service credit cap", account_id=NORTHSTAR, limit=10
    )
    governing = decision.governing_for(Topic.SERVICE_CREDIT)

    assert governing
    assert governing[0].source_file == NORTHSTAR_PDF
    assert "5,000" in governing[0].text


# --- account isolation -----------------------------------------------------------------------


def test_northstar_scope_never_exposes_lumenworks_agreement(doc_conn):
    results = search_documents(
        doc_conn,
        "LumenWorks fixed INR 300 credit 4 hours Growth plan agreement",
        account_id=NORTHSTAR,
        limit=50,
    )

    assert LUMENWORKS_PDF not in files_of(results)
    assert all(e.account_id in (None, NORTHSTAR) for e in results)


def test_lumenworks_scope_never_exposes_northstar_agreement(doc_conn):
    results = search_documents(
        doc_conn,
        "Northstar Logistics Enterprise 15 minutes P1 no cancellation fee",
        account_id=LUMENWORKS,
        limit=50,
    )

    assert NORTHSTAR_PDF not in files_of(results)
    assert all(e.account_id in (None, LUMENWORKS) for e in results)


def test_account_scope_still_returns_general_documents(doc_conn):
    results = search_documents(
        doc_conn, "cancellation credit severity policy", account_id=NORTHSTAR, limit=50
    )
    general = {e.source_file for e in results if e.account_id is None}

    assert SOP_PDF in general
    assert CURRENT_POLICY_PDF in general


def test_allowed_account_ids_restricts_visible_agreements(doc_conn):
    results = search_documents(
        doc_conn,
        "agreement terms cancellation credit",
        allowed_account_ids={NORTHSTAR},
        limit=50,
    )

    assert LUMENWORKS_PDF not in files_of(results)
    assert all(e.account_id in (None, NORTHSTAR) for e in results)


def test_empty_allowed_account_ids_hides_all_agreements(doc_conn):
    results = search_documents(
        doc_conn, "agreement terms cancellation credit", allowed_account_ids=set(), limit=50
    )

    assert all(e.account_id is None for e in results)
    assert NORTHSTAR_PDF not in files_of(results)
    assert LUMENWORKS_PDF not in files_of(results)


def test_authorization_and_query_scope_compose(doc_conn):
    """Authorized for Northstar only, but asking about LumenWorks: the
    stricter of the two wins and no agreement is returned."""
    results = search_documents(
        doc_conn,
        "agreement credit terms",
        account_id=LUMENWORKS,
        allowed_account_ids={NORTHSTAR},
        limit=50,
    )

    assert all(e.account_id is None for e in results)


def test_unscoped_search_can_see_all_agreements(doc_conn):
    """No auth layer exists yet; an unrestricted caller sees everything, and
    precedence — not visibility — is what stops one customer's terms from
    governing another's question."""
    results = search_documents(doc_conn, "agreement terms cancellation credit", limit=50)

    assert NORTHSTAR_PDF in files_of(results)
    assert LUMENWORKS_PDF in files_of(results)


def test_no_cross_account_leakage_across_the_whole_corpus(doc_conn):
    """Sweep every chunk with a broad query under each account scope."""
    broad = "astrion policy agreement cancellation credit pickup support shipment"

    for scope, forbidden in ((NORTHSTAR, LUMENWORKS_PDF), (LUMENWORKS, NORTHSTAR_PDF)):
        results = search_documents(doc_conn, broad, account_id=scope, limit=100)
        assert forbidden not in files_of(results)


# --- get_document_evidence ----------------------------------------------------------------------


def test_get_document_evidence_by_document_returns_ordered_chunks(doc_conn):
    evidence = get_document_evidence(doc_conn, document_id=CURRENT_POLICY_DOC_ID)

    assert evidence
    assert all(e.source_file == CURRENT_POLICY_PDF for e in evidence)
    assert [e.chunk_id for e in evidence] == sorted(
        (e.chunk_id for e in evidence), key=lambda c: int(c.rsplit("c", 1)[1])
    )


def test_get_document_evidence_by_chunk_ids_round_trips(doc_conn):
    found = search_documents(doc_conn, "order cancellation 30 minutes", limit=3)
    ids = [e.chunk_id for e in found]

    refetched = get_document_evidence(doc_conn, chunk_ids=ids)

    assert {e.chunk_id for e in refetched} == set(ids)
    assert all(e.score is None for e in refetched)  # identity fetch, not ranked


def test_get_document_evidence_respects_account_scope(doc_conn):
    lumenworks_chunks = get_document_evidence(doc_conn, document_id=LUMENWORKS_DOC_ID)
    assert lumenworks_chunks

    blocked = get_document_evidence(
        doc_conn, document_id=LUMENWORKS_DOC_ID, account_id=NORTHSTAR
    )
    assert blocked == []

    blocked_by_id = get_document_evidence(
        doc_conn, chunk_ids=[lumenworks_chunks[0].chunk_id], account_id=NORTHSTAR
    )
    assert blocked_by_id == []


def test_get_document_evidence_unknown_ids_return_empty(doc_conn):
    assert get_document_evidence(doc_conn, document_id="no_such_document") == []
    assert get_document_evidence(doc_conn, chunk_ids=["no#such#chunk"]) == []
    assert get_document_evidence(doc_conn, chunk_ids=[]) == []


def test_get_document_evidence_requires_exactly_one_selector(doc_conn):
    with pytest.raises(ValueError, match="exactly one"):
        get_document_evidence(doc_conn)
    with pytest.raises(ValueError, match="exactly one"):
        get_document_evidence(doc_conn, document_id="x", chunk_ids=["y"])


# --- untrusted input safety -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "'; DROP TABLE documents; --",
        "' OR '1'='1",
        "x'); DELETE FROM document_chunks; --",
        'foo" UNION SELECT * FROM accounts --',
        "%' OR account_id IS NOT NULL --",
    ],
)
def test_injection_payloads_in_query_are_inert(doc_conn, payload):
    search_documents(doc_conn, payload, account_id=NORTHSTAR, limit=10)

    # Tables survive and still hold their rows.
    assert doc_conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 6
    assert doc_conn.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0] > 0


def test_injection_payload_cannot_widen_account_scope(doc_conn):
    results = search_documents(
        doc_conn,
        "LumenWorks' OR account_id='ACCT-002",
        account_id=NORTHSTAR,
        limit=50,
    )

    assert LUMENWORKS_PDF not in files_of(results)


@pytest.mark.parametrize(
    "payload", ["ACCT-001' OR '1'='1", "'; DROP TABLE documents; --", "ACCT-%"]
)
def test_injection_payloads_in_account_id_are_inert(doc_conn, payload):
    results = search_documents(doc_conn, "cancellation credit", account_id=payload, limit=50)

    # Treated as a literal (non-matching) account id: general documents only.
    assert all(e.account_id is None for e in results)
    assert doc_conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 6


def test_injection_payload_in_chunk_id_is_inert(doc_conn):
    assert get_document_evidence(doc_conn, chunk_ids=["x'; DROP TABLE documents; --"]) == []
    assert doc_conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 6


def test_very_long_query_is_handled(doc_conn):
    results = search_documents(doc_conn, "cancellation " * 5000, limit=5)

    assert isinstance(results, list)
