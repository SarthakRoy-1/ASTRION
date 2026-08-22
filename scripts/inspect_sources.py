"""Inspect the assessment source pack and report its real structure.

Reads every PDF (via PyMuPDF) and the XLSX workbook (via openpyxl) under
data/source/, and writes a machine-readable inspection report to
data/processed/. This script only observes; it never modifies source files
and it never writes anything back into data/source/.

Phase 1 scope: structural discovery only. No database, no policy logic, no
retrieval, no chunking decisions are made here.

Usage:
    python scripts/inspect_sources.py [--json]

Exit codes:
    0  inspection completed and report written
    1  a source file could not be opened/inspected, or verification failed
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import openpyxl
import pymupdf

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = REPO_ROOT / "data" / "source"
PROCESSED_DIR = REPO_ROOT / "data" / "processed"
OUTPUT_PATH = PROCESSED_DIR / "source_inspection.json"

PDF_FILES = (
    "01_Support_Policy_v3_CURRENT.pdf",
    "02_Support_Policy_v2_DEPRECATED.pdf",
    "03_Cancellation_and_Service_Credit_SOP_v4.pdf",
    "04_Product_Operations_Guide_and_Known_Issues.pdf",
    "05_Northstar_Logistics_Enterprise_Agreement.pdf",
    "06_LumenWorks_Service_Agreement.pdf",
)
XLSX_FILE = "ParcelPilot_Assessment_Data.xlsx"

# Cap on how much of a value is echoed back in the report, so a long free-text
# cell (a ticket description, a note) doesn't dump unnecessarily large or
# sensitive-feeling content into the artifact.
MAX_SAMPLE_CHARS = 120
MAX_SAMPLE_VALUES_PER_COLUMN = 3


def _truncate(value: str, limit: int = MAX_SAMPLE_CHARS) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _safe_sample(value: Any) -> Any:
    """Render a cell value for the report without dumping unbounded text."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return _truncate(str(value))


def inspect_pdf(path: Path) -> dict:
    entry: dict = {
        "name": path.name,
        "readable": False,
        "page_count": None,
        "pages": [],
        "total_chars": 0,
        "total_words": 0,
        "is_deprecated": "DEPRECATED" in path.stem.upper(),
        "error": None,
    }
    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # pymupdf raises its own exception types
        entry["error"] = str(exc)
        return entry

    try:
        entry["readable"] = True
        entry["page_count"] = doc.page_count
        for page_index in range(doc.page_count):
            page = doc[page_index]
            text = page.get_text()
            words = text.split()
            # get_text("words") gives PyMuPDF's own word/box extraction, used
            # here only as a signal of whether text extraction is meaningful
            # (vs. e.g. a scanned/image-only page yielding ~no text).
            extracted_words = page.get_text("words")
            entry["pages"].append(
                {
                    "page_number": page_index + 1,
                    "char_count": len(text),
                    "word_count": len(words),
                    "extracted_word_boxes": len(extracted_words),
                    "has_extractable_text": len(text.strip()) > 0,
                    "image_count": len(page.get_images(full=True)),
                }
            )
            entry["total_chars"] += len(text)
            entry["total_words"] += len(words)
    finally:
        doc.close()

    return entry


def _column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def inspect_sheet(ws) -> dict:
    rows = list(ws.iter_rows(values_only=True))
    header = list(rows[0]) if rows else []
    data_rows = rows[1:] if len(rows) > 1 else []

    columns: list[dict] = []
    for col_index, col_name in enumerate(header):
        values = [r[col_index] if col_index < len(r) else None for r in data_rows]
        non_null = [v for v in values if v is not None and v != ""]
        null_count = len(values) - len(non_null)
        observed_types = sorted({type(v).__name__ for v in non_null})
        samples = []
        seen = set()
        for v in non_null:
            s = _safe_sample(v)
            key = repr(s)
            if key in seen:
                continue
            seen.add(key)
            samples.append(s)
            if len(samples) >= MAX_SAMPLE_VALUES_PER_COLUMN:
                break

        col_name_str = str(col_name) if col_name is not None else f"(unnamed:{_column_letter(col_index)})"
        columns.append(
            {
                "name": col_name_str,
                "spreadsheet_column": _column_letter(col_index),
                "non_null_count": len(non_null),
                "null_count": null_count,
                "null_fraction": round(null_count / len(values), 4) if values else None,
                "distinct_non_null_count": len({repr(v) for v in non_null}),
                "observed_python_types": observed_types,
                "sample_values": samples,
                "all_values_unique": len(non_null) == len({repr(v) for v in non_null})
                and len(non_null) == len(values)
                and len(values) > 0,
            }
        )

    return {
        "row_count": len(data_rows),
        "column_count": len(header),
        "columns": columns,
    }


def inspect_readme_sheet(wb) -> dict | None:
    if "README" not in wb.sheetnames:
        return None
    ws = wb["README"]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    kv: dict[str, Any] = {}
    for row in rows:
        if len(row) >= 2 and row[0] is not None:
            kv[str(row[0])] = _safe_sample(row[1]) if row[1] is not None else None
    return {"raw_rows": [[_safe_sample(c) for c in row] for row in rows], "key_values": kv}


def find_snapshot_timestamp(readme_info: dict | None) -> str | None:
    if not readme_info:
        return None
    kv = readme_info.get("key_values", {})
    for key, value in kv.items():
        if "snapshot" in key.lower() and value:
            return str(value)
    return None


def infer_foreign_keys(sheets: dict[str, dict]) -> list[dict]:
    """Observation only: flag columns whose name matches another sheet's
    apparent primary key (id-suffixed column with fully unique values)."""
    primary_keys: dict[str, str] = {}
    for sheet_name, sheet in sheets.items():
        for col in sheet["columns"]:
            if col["name"].endswith("_id") and col["all_values_unique"] and col["null_count"] == 0:
                # Prefer a column literally named "<singular>_id" matching the
                # sheet name (e.g. accounts.account_id) as that sheet's key.
                primary_keys.setdefault(sheet_name, col["name"])

    relationships = []
    for sheet_name, sheet in sheets.items():
        for col in sheet["columns"]:
            for other_sheet, pk_name in primary_keys.items():
                if other_sheet == sheet_name:
                    continue
                if col["name"] == pk_name and col["name"] != f"{sheet_name.rstrip('s')}_id":
                    relationships.append(
                        {
                            "from_sheet": sheet_name,
                            "from_column": col["name"],
                            "likely_references_sheet": other_sheet,
                            "likely_references_column": pk_name,
                            "basis": "column name matches another sheet's unique *_id column",
                        }
                    )
    return relationships


def inspect_xlsx(path: Path) -> dict:
    entry: dict = {
        "name": path.name,
        "readable": False,
        "sheet_names": [],
        "sheets": {},
        "dataset_snapshot": None,
        "observed_relationships": [],
        "error": None,
    }
    try:
        wb = openpyxl.load_workbook(path, data_only=True)
    except Exception as exc:
        entry["error"] = str(exc)
        return entry

    entry["readable"] = True
    entry["sheet_names"] = list(wb.sheetnames)

    readme_info = inspect_readme_sheet(wb)
    if readme_info is not None:
        entry["readme_sheet"] = readme_info
        entry["dataset_snapshot"] = find_snapshot_timestamp(readme_info)

    data_sheets = {}
    for sheet_name in wb.sheetnames:
        if sheet_name == "README":
            continue
        data_sheets[sheet_name] = inspect_sheet(wb[sheet_name])
    entry["sheets"] = data_sheets

    entry["observed_relationships"] = infer_foreign_keys(data_sheets)

    return entry


def build_report(source_dir: Path) -> dict:
    report: dict = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_dir": str(source_dir),
        "ok": True,
        "pdfs": [],
        "xlsx": None,
    }

    for name in PDF_FILES:
        path = source_dir / name
        if not path.is_file():
            report["ok"] = False
            report["pdfs"].append({"name": name, "readable": False, "error": "file not found"})
            continue
        pdf_entry = inspect_pdf(path)
        if not pdf_entry["readable"]:
            report["ok"] = False
        report["pdfs"].append(pdf_entry)

    xlsx_path = source_dir / XLSX_FILE
    if not xlsx_path.is_file():
        report["ok"] = False
        report["xlsx"] = {"name": XLSX_FILE, "readable": False, "error": "file not found"}
    else:
        xlsx_entry = inspect_xlsx(xlsx_path)
        if not xlsx_entry["readable"]:
            report["ok"] = False
        report["xlsx"] = xlsx_entry

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=SOURCE_DIR)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--json", action="store_true", help="Print the JSON report to stdout too")
    args = parser.parse_args(argv)

    report = build_report(args.source_dir)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"Inspected {len(report['pdfs'])} PDF(s) and 1 workbook.")
        for pdf in report["pdfs"]:
            status = "OK" if pdf.get("readable") else "FAIL"
            pages = pdf.get("page_count")
            print(f"  [{status}] {pdf['name']:55s} pages={pages}")
        xlsx = report["xlsx"]
        if xlsx and xlsx.get("readable"):
            print(f"  [OK] {xlsx['name']} sheets={xlsx['sheet_names']}")
            for sheet_name, sheet in xlsx["sheets"].items():
                print(f"       {sheet_name}: rows={sheet['row_count']} cols={sheet['column_count']}")
            print(f"  dataset snapshot: {xlsx.get('dataset_snapshot')}")
        else:
            print(f"  [FAIL] {XLSX_FILE}: {xlsx.get('error') if xlsx else 'not found'}")
        print(f"\nReport written to: {args.output}")
        print("INSPECTION OK" if report["ok"] else "INSPECTION FAILED")

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
