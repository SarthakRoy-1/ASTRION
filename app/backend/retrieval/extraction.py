"""Deterministic PDF → document metadata + section-aware chunks.

Pure extraction: this module reads a PDF and returns data. It never touches
the database and never writes to data/source/.

**How structure is recovered.** Every supplied PDF uses the same typographic
convention, confirmed by inspecting the real files rather than assumed:

    24pt bold  document title (may wrap onto a second line)
    18pt bold  top-level section heading  ("1. Order cancellation";
               02_Support_Policy_v2_DEPRECATED.pdf's is unnumbered)
    14pt bold  sub-section heading        ("KI-208 - Bulk Upload failures…")
    11pt bold  `Key: Value` preamble metadata, and in-body labels
    11pt       body text

So headings are detected by **font size + boldness**, not by a numbering
regex — 02's heading carries no number, and a size rule handles it. Numbering
is parsed when present and left null when absent rather than invented.

**Why metadata is read only from the preamble.** 04_Product_Operations_Guide
contains `Status: Investigating` and `Status: Monitoring` *inside* its
known-issue blocks. Reading `Status:` from anywhere on the page would
misclassify that document's authority. Only lines above the first section
heading are treated as document metadata.

**Chunk boundaries.** A chunk starts at every section or sub-section heading,
and never spans a page — so a chunk's page number is always exact. A section
continuing across a page break yields two chunks carrying the same section
title. Sub-sections become their own chunks (KI-208 and KI-211 are separately
retrievable) while retaining their parent section for citation.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pymupdf

from app.backend.models.documents import Document, DocumentChunk
from app.backend.retrieval.authority import (
    UnknownAuthorityError,
    authority_tier_for,
    classify_document_type,
    classify_topic,
    is_authoritative,
    normalise_status,
)

# Font sizes observed in the supplied pack (body text is 11pt throughout).
TITLE_SIZE_MIN = 20.0
SECTION_SIZE_MIN = 18.0
SUBSECTION_SIZE_MIN = 14.0

# Invisible characters PyMuPDF returns from these PDFs. The bullet glyph in
# the source is "●" followed by a zero-width space; stripping the zero-width
# characters is a lossless cleanup (they render as nothing), and keeps chunk
# text usable as a search index and as quoted evidence.
_INVISIBLE = str.maketrans(
    {
        "​": "",  # zero-width space (follows every bullet glyph in these PDFs)
        "﻿": "",  # zero-width no-break space
        "­": "",  # soft hyphen
        " ": " ",  # non-breaking space -> ordinary space
    }
)

_HEADING_NUMBER_RE = re.compile(r"^(\d+(?:\.\d+)*)\.\s+(.*)$")
_METADATA_RE = re.compile(r"^([A-Za-z][A-Za-z ]{0,30}):\s*(.+)$")


class DocumentIngestionError(Exception):
    """A source document is missing, unreadable, or does not carry the
    metadata the authority model requires. Always raised, never defaulted —
    guessing a document's status would guess its authority."""


@dataclass(frozen=True)
class _Line:
    text: str
    size: float
    bold: bool


@dataclass(frozen=True)
class ExtractedDocument:
    document: Document
    chunks: list[DocumentChunk]
    page_texts: list[str]


def _normalise(text: str) -> str:
    text = text.translate(_INVISIBLE).replace("\t", " ")
    return re.sub(r" {2,}", " ", text).strip()


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def document_id_for(source_file: str) -> str:
    """Stable, readable identifier derived from the filename.

    `01_Support_Policy_v3_CURRENT.pdf` → `01_support_policy_v3_current`.
    Position-based rather than content-hashed, so re-ingesting an unchanged
    corpus reproduces identical ids and existing citations keep resolving.
    """
    stem = Path(source_file).stem.lower()
    return re.sub(r"[^a-z0-9]+", "_", stem).strip("_")


def chunk_id_for(document_id: str, page_number: int, ordinal: int) -> str:
    return f"{document_id}#p{page_number}#c{ordinal:02d}"


def _is_bold(span: dict) -> bool:
    # Bit 4 of PyMuPDF's span flags is "bold"; the font-name check also covers
    # PDFs written by tooling that does not set the flag (e.g. test fixtures).
    return bool(span.get("flags", 0) & (1 << 4)) or "bold" in span.get("font", "").lower()


def _load_lines(doc: pymupdf.Document) -> list[list[_Line]]:
    """Text lines per page, in reading order, with size/bold retained.

    Note: PyMuPDF linearises the response-target tables in the policy PDFs
    row-major (header cells, then each plan's row), so the values survive but
    the grid shape does not. Acceptable here — every number remains present
    and adjacent to its row label.
    """
    pages: list[list[_Line]] = []
    for page_index in range(doc.page_count):
        page = doc[page_index]
        lines: list[_Line] = []
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:  # non-text block (image)
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                text = _normalise("".join(s.get("text", "") for s in spans))
                if not text:
                    continue
                lines.append(
                    _Line(
                        text=text,
                        size=max(float(s.get("size", 0.0)) for s in spans),
                        bold=any(_is_bold(s) for s in spans),
                    )
                )
        pages.append(lines)
    return pages


def _heading_level(line: _Line) -> int | None:
    """0 = document title, 1 = section, 2 = sub-section, None = body."""
    if not line.bold:
        return None
    if line.size >= TITLE_SIZE_MIN:
        return 0
    if line.size >= SECTION_SIZE_MIN:
        return 1
    if line.size >= SUBSECTION_SIZE_MIN:
        return 2
    return None


def _parse_section_heading(text: str) -> tuple[str | None, str]:
    match = _HEADING_NUMBER_RE.match(text)
    if match:
        return match.group(1), match.group(2).strip()
    return None, text.strip()


def _page_text_and_spans(lines: list[_Line]) -> tuple[str, list[tuple[int, int]]]:
    """Join lines with newlines and report each line's (start, end) offset, so
    a chunk's recorded span always satisfies page_text[start:end] == chunk.text."""
    spans: list[tuple[int, int]] = []
    position = 0
    for line in lines:
        spans.append((position, position + len(line.text)))
        position += len(line.text) + 1  # +1 for the joining newline
    return "\n".join(line.text for line in lines), spans


def _extract_preamble_metadata(lines: list[_Line]) -> tuple[str, dict[str, str]]:
    """Title and `Key: Value` pairs from the lines above the first section
    heading. Returns ("", {}) shapes rather than inventing defaults."""
    title_parts: list[str] = []
    metadata: dict[str, str] = {}
    for line in lines:
        level = _heading_level(line)
        if level in (1, 2):
            break
        if level == 0:
            title_parts.append(line.text)
            continue
        match = _METADATA_RE.match(line.text)
        if match:
            key = match.group(1).strip().lower()
            metadata.setdefault(key, match.group(2).strip())
    return " ".join(title_parts).strip(), metadata


def _parse_doc_date(raw: str, *, field: str, source_file: str) -> date:
    try:
        return datetime.strptime(raw.strip(), "%d %B %Y").date()
    except ValueError as exc:
        raise DocumentIngestionError(
            f"{source_file}: '{field}' value {raw!r} is not a recognised date "
            f"(expected e.g. '1 May 2026')"
        ) from exc


def _parse_term(raw: str, *, source_file: str) -> tuple[date | None, date | None]:
    parts = [p.strip() for p in re.split(r"\bto\b", raw, maxsplit=1)]
    if len(parts) != 2:
        return None, None
    return (
        _parse_doc_date(parts[0], field="Term (start)", source_file=source_file),
        _parse_doc_date(parts[1], field="Term (end)", source_file=source_file),
    )


def extract_page_texts(pdf_path: Path) -> list[str]:
    """The normalised text of each page, byte-identical to what chunk spans
    index into. Used to verify recorded provenance against the real PDF."""
    doc = _open(pdf_path)
    try:
        return [_page_text_and_spans(lines)[0] for lines in _load_lines(doc)]
    finally:
        doc.close()


def _open(pdf_path: Path) -> pymupdf.Document:
    if not pdf_path.is_file():
        raise DocumentIngestionError(f"source document not found: {pdf_path}")
    try:
        return pymupdf.open(pdf_path)
    except Exception as exc:  # pymupdf raises its own exception types
        raise DocumentIngestionError(f"{pdf_path.name}: could not be opened as a PDF ({exc})") from exc


def extract_document(pdf_path: Path, *, source_sha256: str | None = None) -> ExtractedDocument:
    """Read one PDF into a Document plus its section-aware chunks.

    Raises DocumentIngestionError if the file is missing, unreadable, has no
    extractable text, or lacks the title/status the authority model needs.
    """
    pdf_path = Path(pdf_path)
    doc = _open(pdf_path)
    try:
        pages_lines = _load_lines(doc)
        page_count = doc.page_count
    finally:
        doc.close()

    source_file = pdf_path.name
    if not any(pages_lines):
        raise DocumentIngestionError(f"{source_file}: no extractable text found")

    title, metadata = _extract_preamble_metadata(pages_lines[0])
    if not title:
        raise DocumentIngestionError(f"{source_file}: no document title found")

    status_raw = metadata.get("status")
    if status_raw is None:
        raise DocumentIngestionError(
            f"{source_file}: preamble states no 'Status:' — authority cannot be determined"
        )

    account_id = metadata.get("account")
    try:
        status = normalise_status(status_raw)
        document_type = classify_document_type(title=title, has_account=account_id is not None)
    except UnknownAuthorityError as exc:
        raise DocumentIngestionError(f"{source_file}: {exc}") from exc

    effective_raw = metadata.get("effective")
    updated_raw = metadata.get("updated")
    term_raw = metadata.get("term")
    term_start, term_end = (
        _parse_term(term_raw, source_file=source_file) if term_raw else (None, None)
    )

    document = Document(
        document_id=document_id_for(source_file),
        source_file=source_file,
        source_sha256=source_sha256 or _sha256_of(pdf_path),
        title=title,
        document_type=document_type,
        status=status,
        status_raw=status_raw,
        is_current=is_authoritative(status),
        is_deprecated=status.value == "DEPRECATED",
        is_authoritative=is_authoritative(status),
        authority_tier=authority_tier_for(document_type, status),
        account_id=account_id,
        customer_name=metadata.get("customer"),
        plan=metadata.get("plan"),
        effective_date_raw=effective_raw,
        effective_date=(
            _parse_doc_date(effective_raw, field="Effective", source_file=source_file)
            if effective_raw
            else None
        ),
        updated_date_raw=updated_raw,
        updated_date=(
            _parse_doc_date(updated_raw, field="Updated", source_file=source_file)
            if updated_raw
            else None
        ),
        term_raw=term_raw,
        term_start=term_start,
        term_end=term_end,
        supersedes=metadata.get("supersedes"),
        superseded_by=metadata.get("superseded by"),
        page_count=page_count,
    )

    chunks, page_texts = _build_chunks(document.document_id, pages_lines)
    if not chunks:
        raise DocumentIngestionError(f"{source_file}: produced no chunks")

    return ExtractedDocument(document=document, chunks=chunks, page_texts=page_texts)


def _build_chunks(
    document_id: str, pages_lines: list[list[_Line]]
) -> tuple[list[DocumentChunk], list[str]]:
    chunks: list[DocumentChunk] = []
    page_texts: list[str] = []
    ordinal = 0

    # Section context carries across page boundaries so a section continuing
    # onto a new page keeps its heading in the chunk's provenance.
    section_number: str | None = None
    section_title: str | None = None
    subsection_title: str | None = None

    for page_index, lines in enumerate(pages_lines):
        page_number = page_index + 1
        page_text, spans = _page_text_and_spans(lines)
        page_texts.append(page_text)

        group: list[int] = []
        group_meta = (section_number, section_title, subsection_title)

        def flush() -> None:
            nonlocal ordinal, group
            if not group:
                return
            text = "\n".join(lines[i].text for i in group)
            start, end = spans[group[0]][0], spans[group[-1]][1]
            number, title, subtitle = group_meta
            chunks.append(
                DocumentChunk(
                    chunk_id=chunk_id_for(document_id, page_number, ordinal),
                    document_id=document_id,
                    chunk_ordinal=ordinal,
                    page_number=page_number,
                    section_number=number,
                    section_title=title,
                    subsection_title=subtitle,
                    section_path=_section_path(number, title, subtitle),
                    topic=classify_topic(subtitle or title, title),
                    text=text,
                    char_count=len(text),
                    word_count=len(text.split()),
                    page_char_start=start,
                    page_char_end=end,
                )
            )
            ordinal += 1
            group = []

        for index, line in enumerate(lines):
            level = _heading_level(line)
            if level in (1, 2):
                flush()
                if level == 1:
                    section_number, section_title = _parse_section_heading(line.text)
                    subsection_title = None
                else:
                    subsection_title = line.text
                group_meta = (section_number, section_title, subsection_title)
            group.append(index)

        flush()

    return chunks, page_texts


def _section_path(number: str | None, title: str | None, subtitle: str | None) -> str | None:
    if not title:
        return None
    head = f"{number}. {title}" if number else title
    return f"{head} > {subtitle}" if subtitle else head
