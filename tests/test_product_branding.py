"""The product is Astrion; what it *quotes* keeps its own names.

ParcelPilot is also the fictional company in the supplied source pack: its
policy PDFs, its workbook and its test fixtures legitimately carry that name, and
a document's title is its content, not the product's label. What must never say
ParcelPilot is code that speaks for the product: the UI, its metadata, and the
messages and emails the API sends.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OLD_NAME = re.compile(r"parcel\s*pilot", re.IGNORECASE)

#: Places that speak for the product.
PRODUCT_FACING = [
    REPO / "app" / "backend",
    REPO / "app" / "frontend" / "src",
    REPO / "app" / "frontend" / "public",
    REPO / "app" / "frontend" / "package.json",
    REPO / "Dockerfile.backend",
    REPO / "app" / "frontend" / "Dockerfile",
]
#: Quoted source material, never the product's name for itself.
QUOTES_SOURCE_MATERIAL = ("fixtures", ".test.", "node_modules", ".next", "__pycache__")
SUFFIXES = {".py", ".ts", ".tsx", ".css", ".json", ".html", ".svg", ".webmanifest", ""}


def product_facing_files():
    for root in PRODUCT_FACING:
        paths = [root] if root.is_file() else root.rglob("*") if root.exists() else []
        for path in paths:
            if not path.is_file() or path.suffix not in SUFFIXES:
                continue
            if any(marker in path.as_posix() for marker in QUOTES_SOURCE_MATERIAL):
                continue
            yield path


def test_nothing_that_speaks_for_the_product_still_calls_it_parcelpilot():
    offenders = []
    for path in product_facing_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if OLD_NAME.search(line):
                offenders.append(f"{path.relative_to(REPO)}:{number}: {line.strip()[:100]}")
    assert offenders == [], "product-facing old name:\n" + "\n".join(offenders)


def test_the_page_metadata_names_the_product():
    layout = (REPO / "app" / "frontend" / "src" / "app" / "layout.tsx").read_text(encoding="utf-8")
    assert "ASTRION" in layout
    assert not OLD_NAME.search(layout)


def test_source_document_titles_are_left_alone():
    """The pack's own PDFs keep their titles; renaming them would alter evidence."""
    from app.backend.retrieval.extraction import extract_document

    policy = REPO / "data" / "source" / "01_Support_Policy_v3_CURRENT.pdf"
    assert "ParcelPilot" in extract_document(policy).document.title
