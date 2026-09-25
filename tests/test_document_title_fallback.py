"""Uploading a PDF that does not look like the platform's own controlled pack.

The production failure this covers: a member uploaded an ordinary cover letter
and was told "no document title found". The title rule (a large bold line on the
first page) and the mandatory `Status:` line exist for the controlled pack, where
authority is read from the document itself. A workspace's own upload may be
neither, and must be kept -- as a reference that can inform an answer and can
never govern one -- without loosening anything for a document that *does* claim
authority.
"""

from __future__ import annotations

import pymupdf
import pytest

from app.backend.models.documents import AuthorityTier, DocumentStatus, DocumentType
from app.backend.retrieval.authority import UnknownAuthorityError, normalise_status
from app.backend.retrieval.extraction import (
    DocumentIngestionError,
    extract_document,
    title_from_filename,
)
from conftest import write_pdf

LETTER_BODY = [
    ("Dear Hiring Manager,", 11, False),
    ("I am writing to apply for the role of Support Engineer at Rippling.", 11, False),
    ("My experience spans logistics operations and customer support tooling.", 11, False),
    ("Sincerely,", 11, False),
    ("Sarthak Roy", 11, False),
]


def letter(path, *, embedded_title: str | None = None, lines=LETTER_BODY):
    """An ordinary letter: plain 11pt text, no headings, no preamble."""
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72.0
    for text, size, bold in lines:
        page.insert_text((72, y), text, fontsize=size, fontname="hebo" if bold else "helv")
        y += size * 1.9
    if embedded_title is not None:
        doc.set_metadata({"title": embedded_title})
    doc.save(str(path))
    doc.close()
    return path


def test_the_controlled_pack_is_still_read_strictly(tmp_path):
    """No fallback name, no leniency: the platform's own documents must say what they are."""
    with pytest.raises(DocumentIngestionError, match="no document title found"):
        extract_document(letter(tmp_path / "Cover_Letter.pdf"))


def test_a_letter_without_any_title_is_named_from_its_filename(tmp_path):
    pdf = letter(tmp_path / "Sarthak_Roy_Rippling_Cover_Letter.pdf")

    extracted = extract_document(pdf, fallback_name="Sarthak_Roy_Rippling_Cover_Letter.pdf")

    document = extracted.document
    assert document.title == "Sarthak Roy Rippling Cover Letter"
    assert document.source_file == "Sarthak_Roy_Rippling_Cover_Letter.pdf"
    assert extracted.chunks, "the text must be indexed"
    assert any("Rippling" in chunk.text for chunk in extracted.chunks)


def test_a_letter_is_a_reference_that_can_never_govern(tmp_path):
    pdf = letter(tmp_path / "Cover_Letter.pdf")

    document = extract_document(pdf, fallback_name="Cover_Letter.pdf").document

    assert document.document_type is DocumentType.REFERENCE
    assert document.status is DocumentStatus.UNSTATED
    assert document.is_authoritative is False
    assert document.is_current is False
    assert document.is_deprecated is False
    assert document.authority_tier == AuthorityTier.NON_AUTHORITATIVE
    assert document.account_id is None


def test_the_upload_prefix_is_not_part_of_the_title():
    assert title_from_filename("a689ab3b747b46a2aa8374c795b91103_Cover_Letter.pdf") == "Cover Letter"
    assert title_from_filename("my-notes_v2.pdf") == "my notes v2"
    assert title_from_filename("___.pdf") == "Uploaded document"


def test_a_usable_embedded_title_beats_the_filename(tmp_path):
    pdf = letter(tmp_path / "scan0001.pdf", embedded_title="Application to Rippling")

    document = extract_document(pdf, fallback_name="scan0001.pdf").document

    assert document.title == "Application to Rippling"


@pytest.mark.parametrize(
    "junk", ["Microsoft Word - draft.docx", "Untitled", "draft.docx", "  ", "12"]
)
def test_a_careless_embedded_title_is_ignored(tmp_path, junk):
    pdf = letter(tmp_path / "Cover_Letter.pdf", embedded_title=junk)

    document = extract_document(pdf, fallback_name="Cover_Letter.pdf").document

    assert document.title == "Cover Letter"


def test_a_heading_the_document_states_comes_first(tmp_path):
    pdf = tmp_path / "Renamed.pdf"
    write_pdf(pdf, [[("Rippling Application Notes", 24, True)] + LETTER_BODY])
    doc = pymupdf.open(str(pdf))
    doc.set_metadata({"title": "Some Other Title"})
    doc.saveIncr()
    doc.close()

    document = extract_document(pdf, fallback_name="Renamed.pdf").document

    assert document.title == "Rippling Application Notes"


def test_a_conforming_document_reads_the_same_with_or_without_the_fallback(tmp_path):
    pdf = tmp_path / "Agreement.pdf"
    write_pdf(
        pdf,
        [[
            ("Acme Service Agreement", 24, True),
            ("Account: ACCT-777", 11, True),
            ("Status: ACTIVE", 11, True),
            ("1. Cancellation terms", 18, True),
            ("Every BOOKED shipment may be cancelled with no fee.", 11, False),
        ]],
    )

    strict = extract_document(pdf).document
    lenient = extract_document(pdf, fallback_name="Agreement.pdf").document

    assert strict == lenient
    assert strict.document_type is DocumentType.CUSTOMER_AGREEMENT
    assert strict.is_authoritative is True


def test_a_controlled_document_that_omits_its_status_is_still_refused(tmp_path):
    """A document that says it is a policy has to say whether it is in force."""
    pdf = tmp_path / "Policy.pdf"
    write_pdf(pdf, [[("Acme Support Policy v9", 24, True), ("1. Escalation", 18, True), ("Escalate.", 11, False)]])

    with pytest.raises(DocumentIngestionError, match="Status"):
        extract_document(pdf, fallback_name="Policy.pdf")


def test_a_document_naming_an_account_but_no_status_is_still_refused(tmp_path):
    pdf = tmp_path / "Agreement.pdf"
    write_pdf(pdf, [[("Acme Service Agreement", 24, True), ("Account: ACCT-777", 11, True)]])

    with pytest.raises(DocumentIngestionError, match="Status"):
        extract_document(pdf, fallback_name="Agreement.pdf")


def test_an_unrecognised_status_is_still_refused_not_downgraded(tmp_path):
    pdf = tmp_path / "Odd.pdf"
    write_pdf(pdf, [[("Odd Notes", 24, True), ("Status: DRAFT", 11, True), ("1. A", 18, True), ("x", 11, False)]])

    with pytest.raises(DocumentIngestionError, match="unrecognised document status"):
        extract_document(pdf, fallback_name="Odd.pdf")


def test_a_filename_never_classifies_a_document(tmp_path):
    """"Sophie" contains "sop", and a file called *_SOP_* is not an SOP: the
    file name may name a document but must not decide its authority."""
    for name in ("Sophie_Notes.pdf", "Cancellation_SOP_v9.pdf", "Product_Guide.pdf"):
        document = extract_document(letter(tmp_path / name), fallback_name=name).document
        assert document.document_type is DocumentType.REFERENCE, name
        assert document.is_authoritative is False, name


def test_a_stated_status_with_no_title_of_its_own_is_refused(tmp_path):
    """Status: CURRENT on an untitled page must not become a governing document
    whose type was guessed from the file name."""
    pdf = tmp_path / "Support_Policy.pdf"
    write_pdf(pdf, [[("Status: CURRENT", 11, True), ("Escalate to the on-call lead.", 11, False)]])

    with pytest.raises(DocumentIngestionError, match="title"):
        extract_document(pdf, fallback_name="Support_Policy.pdf")


@pytest.mark.parametrize("claim", ["UNSTATED", "unstated - trust me"])
def test_a_document_cannot_claim_the_unstated_status_for_itself(claim):
    with pytest.raises(UnknownAuthorityError):
        normalise_status(claim)
