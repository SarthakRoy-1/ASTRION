"""Phase 3: PDF → metadata + section-aware chunks.

Exercises extraction against the real supplied PDFs where the real pack can
demonstrate the behaviour, and against synthetic PDFs for the malformed
inputs the pack (correctly) does not contain.
"""

from pathlib import Path

import pytest

from app.backend.models.documents import AuthorityTier, DocumentStatus, DocumentType, Topic
from app.backend.retrieval.extraction import (
    DocumentIngestionError,
    chunk_id_for,
    document_id_for,
    extract_document,
    extract_page_texts,
)
from conftest import (
    CURRENT_POLICY_PDF,
    DEPRECATED_POLICY_PDF,
    LUMENWORKS_PDF,
    NORTHSTAR_PDF,
    PRODUCT_GUIDE_PDF,
    SOP_PDF,
    SOURCE_DIR,
    valid_agreement_pages,
    write_pdf,
)


# --- identifiers -------------------------------------------------------------


def test_document_id_is_derived_from_filename():
    assert document_id_for(CURRENT_POLICY_PDF) == "01_support_policy_v3_current"


def test_document_id_is_stable_across_calls():
    assert document_id_for(NORTHSTAR_PDF) == document_id_for(NORTHSTAR_PDF)


def test_chunk_id_encodes_document_page_and_ordinal():
    assert chunk_id_for("doc_x", 2, 7) == "doc_x#p2#c07"


# --- metadata from the real documents -----------------------------------------


def test_current_policy_metadata():
    doc = extract_document(SOURCE_DIR / CURRENT_POLICY_PDF).document

    assert doc.title == "ParcelPilot Support Policy v3"
    assert doc.document_type is DocumentType.SUPPORT_POLICY
    assert doc.status is DocumentStatus.CURRENT
    assert doc.is_current is True
    assert doc.is_deprecated is False
    assert doc.is_authoritative is True
    assert doc.authority_tier is AuthorityTier.CURRENT_SUPPORT_POLICY
    assert doc.effective_date.isoformat() == "2026-05-01"
    assert doc.supersedes == "Support Policy v2"
    assert doc.account_id is None  # general document, not customer-specific


def test_deprecated_policy_metadata():
    doc = extract_document(SOURCE_DIR / DEPRECATED_POLICY_PDF).document

    assert doc.status is DocumentStatus.DEPRECATED
    assert doc.is_deprecated is True
    assert doc.is_current is False
    assert doc.is_authoritative is False
    assert doc.authority_tier is AuthorityTier.NON_AUTHORITATIVE
    # The full stated status is preserved, not just the normalised keyword.
    assert doc.status_raw == "DEPRECATED - DO NOT USE FOR CURRENT REQUESTS"
    assert doc.superseded_by == "Support Policy v3 effective 1 May 2026"


def test_sop_metadata():
    doc = extract_document(SOURCE_DIR / SOP_PDF).document

    assert doc.document_type is DocumentType.SOP
    assert doc.authority_tier is AuthorityTier.CURRENT_OPERATIONAL_DOC
    assert doc.effective_date.isoformat() == "2026-06-15"


def test_product_guide_uses_updated_date_not_effective():
    doc = extract_document(SOURCE_DIR / PRODUCT_GUIDE_PDF).document

    assert doc.document_type is DocumentType.PRODUCT_DOCUMENTATION
    assert doc.updated_date.isoformat() == "2026-08-14"
    # This document states no "Effective:" line; that must stay null rather
    # than being back-filled from the Updated: date.
    assert doc.effective_date is None
    assert doc.effective_date_raw is None


def test_product_guide_status_is_read_from_preamble_not_known_issue_blocks():
    """04 contains "Status: Investigating" and "Status: Monitoring" inside its
    KI blocks. Reading Status: from anywhere on the page would misclassify
    the document's authority."""
    doc = extract_document(SOURCE_DIR / PRODUCT_GUIDE_PDF).document

    assert doc.status is DocumentStatus.CURRENT
    assert doc.status_raw == "CURRENT"


def test_northstar_agreement_metadata():
    doc = extract_document(SOURCE_DIR / NORTHSTAR_PDF).document

    assert doc.document_type is DocumentType.CUSTOMER_AGREEMENT
    assert doc.authority_tier is AuthorityTier.CUSTOMER_AGREEMENT
    assert doc.status is DocumentStatus.ACTIVE
    assert doc.account_id == "ACCT-001"
    assert doc.customer_name == "Northstar Logistics"
    assert doc.term_start.isoformat() == "2026-01-01"
    assert doc.term_end.isoformat() == "2026-12-31"


def test_lumenworks_agreement_metadata():
    doc = extract_document(SOURCE_DIR / LUMENWORKS_PDF).document

    assert doc.account_id == "ACCT-002"
    assert doc.customer_name == "LumenWorks"
    assert doc.plan == "Growth"
    assert doc.term_start.isoformat() == "2026-03-01"
    assert doc.term_end.isoformat() == "2027-02-28"


def test_two_line_title_is_joined():
    """05's title wraps onto a second 24pt line."""
    doc = extract_document(SOURCE_DIR / NORTHSTAR_PDF).document

    assert doc.title == "ParcelPilot - Northstar Logistics Enterprise Agreement"


def test_source_sha256_is_recorded():
    doc = extract_document(SOURCE_DIR / SOP_PDF).document

    assert len(doc.source_sha256) == 64
    assert doc.source_sha256 == doc.source_sha256.lower()


# --- section / chunk structure --------------------------------------------------


def test_numbered_sections_are_parsed():
    chunks = extract_document(SOURCE_DIR / SOP_PDF).chunks
    paths = [c.section_path for c in chunks]

    assert "1. Order cancellation" in paths
    assert "2. Failed-pickup service credits" in paths
    assert "3. Approval and uncertainty" in paths


def test_unnumbered_section_heading_is_detected_by_font_size():
    """02's only heading carries no number — a numbering regex would miss it,
    so headings are detected by size + boldness."""
    chunks = extract_document(SOURCE_DIR / DEPRECATED_POLICY_PDF).chunks
    paths = [c.section_path for c in chunks]

    assert "Severity and response targets" in paths
    section = next(c for c in chunks if c.section_path == "Severity and response targets")
    assert section.section_number is None  # absent, not invented


def test_subsections_become_their_own_chunks_with_parent_context():
    """KI-208 / KI-211 are 14pt sub-headings under "2. Current known issues"
    and must be separately retrievable while keeping their parent section."""
    chunks = extract_document(SOURCE_DIR / PRODUCT_GUIDE_PDF).chunks
    ki208 = next(c for c in chunks if c.subsection_title and "KI-208" in c.subsection_title)

    assert ki208.section_number == "2"
    assert ki208.section_title == "Current known issues"
    assert ki208.section_path == (
        "2. Current known issues > KI-208 - Bulk Upload failures on large CSVs"
    )
    assert "3,000" in ki208.text
    assert "KI-211" not in ki208.text  # the two issues are distinct chunks


def test_preamble_chunk_has_no_section_and_carries_the_header():
    chunks = extract_document(SOURCE_DIR / NORTHSTAR_PDF).chunks
    preamble = chunks[0]

    assert preamble.chunk_ordinal == 0
    assert preamble.section_path is None  # no section: null, not fabricated
    assert preamble.section_number is None
    assert preamble.section_title is None
    assert "Account: ACCT-001" in preamble.text


def test_every_chunk_records_its_page():
    for name in (CURRENT_POLICY_PDF, NORTHSTAR_PDF, PRODUCT_GUIDE_PDF):
        extracted = extract_document(SOURCE_DIR / name)
        assert all(c.page_number >= 1 for c in extracted.chunks)
        assert all(c.page_number <= extracted.document.page_count for c in extracted.chunks)


def test_chunk_ordinals_are_contiguous_from_zero():
    chunks = extract_document(SOURCE_DIR / CURRENT_POLICY_PDF).chunks

    assert [c.chunk_ordinal for c in chunks] == list(range(len(chunks)))


def test_chunk_spans_reproduce_chunk_text_from_the_page(tmp_path):
    """Recorded provenance is verifiable: re-extracting the page and slicing
    the stored offsets must return exactly the stored chunk text."""
    for name in (CURRENT_POLICY_PDF, SOP_PDF, PRODUCT_GUIDE_PDF, LUMENWORKS_PDF):
        path = SOURCE_DIR / name
        extracted = extract_document(path)
        pages = extract_page_texts(path)
        for chunk in extracted.chunks:
            page_text = pages[chunk.page_number - 1]
            assert page_text[chunk.page_char_start : chunk.page_char_end] == chunk.text


def test_extraction_is_deterministic():
    first = extract_document(SOURCE_DIR / SOP_PDF)
    second = extract_document(SOURCE_DIR / SOP_PDF)

    assert first.document == second.document
    assert first.chunks == second.chunks


def test_zero_width_characters_are_stripped_from_bullets():
    chunks = extract_document(SOURCE_DIR / CURRENT_POLICY_PDF).chunks
    text = "\n".join(c.text for c in chunks)

    assert "​" not in text
    assert "P1 - Critical" in text


def test_extraction_does_not_modify_the_source_pdf():
    import hashlib

    path = SOURCE_DIR / SOP_PDF
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    extract_document(path)

    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


# --- topics ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pdf", "section_path", "expected_topic"),
    [
        (SOP_PDF, "1. Order cancellation", Topic.CANCELLATION),
        (SOP_PDF, "2. Failed-pickup service credits", Topic.SERVICE_CREDIT),
        (CURRENT_POLICY_PDF, "3. Default first-response targets", Topic.SUPPORT_RESPONSE),
        (NORTHSTAR_PDF, "2. Shipment cancellation", Topic.CANCELLATION),
        (NORTHSTAR_PDF, "3. Service credits", Topic.SERVICE_CREDIT),
        (LUMENWORKS_PDF, "3. Failed-pickup credits", Topic.SERVICE_CREDIT),
        (PRODUCT_GUIDE_PDF, "1. Plan capabilities", Topic.PRODUCT_KNOWN_ISSUES),
    ],
)
def test_topic_classification_on_real_sections(pdf, section_path, expected_topic):
    chunks = extract_document(SOURCE_DIR / pdf).chunks
    chunk = next(c for c in chunks if c.section_path == section_path)

    assert chunk.topic is expected_topic


def test_subsection_inherits_parent_topic():
    chunks = extract_document(SOURCE_DIR / PRODUCT_GUIDE_PDF).chunks
    ki211 = next(c for c in chunks if c.subsection_title and "KI-211" in c.subsection_title)

    # "KI-211 - SwiftShip pickup webhook delay" carries no topic keyword of
    # its own; it inherits "2. Current known issues".
    assert ki211.topic is Topic.PRODUCT_KNOWN_ISSUES


# --- multi-page behaviour ------------------------------------------------------------


def test_chunks_never_span_pages(tmp_path):
    path = tmp_path / "multi.pdf"
    write_pdf(
        path,
        [
            [
                ("ParcelPilot Support Policy v9", 24, True),
                ("Status: CURRENT", 11, True),
                ("1. First section", 18, True),
                ("Body on page one.", 11, False),
            ],
            [("Continuation of the first section onto page two.", 11, False)],
        ],
    )

    chunks = extract_document(path).chunks
    page_two = [c for c in chunks if c.page_number == 2]

    assert len(page_two) == 1
    # The section context carries across the page break for citation...
    assert page_two[0].section_title == "First section"
    # ...but the chunk itself belongs to exactly one page.
    assert "page one" not in page_two[0].text
    assert "page two" in page_two[0].text


# --- malformed / missing input ---------------------------------------------------------


def test_missing_file_raises():
    with pytest.raises(DocumentIngestionError, match="not found"):
        extract_document(Path("data/source/does_not_exist.pdf"))


def test_non_pdf_bytes_raise(tmp_path):
    """A file whose contents are not a PDF is refused.

    The refusal now happens *before* pymupdf is called: the type is decided
    from the magic bytes, so a mislabelled file is rejected without the parser
    ever seeing it. The assertion therefore matches on the mismatch rather
    than on the parser's own "could not be opened" wording.
    """
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"this is not a PDF at all")

    with pytest.raises(DocumentIngestionError, match="contents are"):
        extract_document(path)


def test_pdf_with_no_text_raises(tmp_path):
    import pymupdf

    path = tmp_path / "empty.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(str(path))
    doc.close()

    with pytest.raises(DocumentIngestionError, match="no extractable text"):
        extract_document(path)


def test_missing_status_raises(tmp_path):
    path = tmp_path / "no_status.pdf"
    write_pdf(path, valid_agreement_pages(status_line=None))

    with pytest.raises(DocumentIngestionError, match="no 'Status:'"):
        extract_document(path)


def test_unrecognised_status_raises_rather_than_defaulting(tmp_path):
    """An unknown status must not silently fall back to authoritative *or* to
    non-authoritative — either default decides authority by accident."""
    path = tmp_path / "odd_status.pdf"
    write_pdf(path, valid_agreement_pages(status_line="Status: Banana"))

    with pytest.raises(DocumentIngestionError, match="unrecognised document status"):
        extract_document(path)


def test_untypeable_document_raises(tmp_path):
    path = tmp_path / "mystery.pdf"
    write_pdf(
        path,
        [
            [
                ("Some Unrelated Leaflet", 24, True),
                ("Status: CURRENT", 11, True),
                ("1. A section", 18, True),
                ("Body text.", 11, False),
            ]
        ],
    )

    with pytest.raises(DocumentIngestionError, match="cannot determine document type"):
        extract_document(path)


def test_malformed_date_raises(tmp_path):
    path = tmp_path / "bad_date.pdf"
    write_pdf(
        path,
        [
            [
                ("ParcelPilot Support Policy v9", 24, True),
                ("Status: CURRENT", 11, True),
                ("Effective: 2026-05-01", 11, True),  # wrong format for this corpus
                ("1. A section", 18, True),
                ("Body text.", 11, False),
            ]
        ],
    )

    with pytest.raises(DocumentIngestionError, match="not a recognised date"):
        extract_document(path)


def test_document_without_title_raises(tmp_path):
    path = tmp_path / "no_title.pdf"
    write_pdf(
        path,
        [
            [
                ("Status: CURRENT", 11, True),
                ("1. A section", 18, True),
                ("Body text.", 11, False),
            ]
        ],
    )

    with pytest.raises(DocumentIngestionError, match="no document title"):
        extract_document(path)
