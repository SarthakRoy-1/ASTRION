import json

import openpyxl
import pymupdf
import pytest

from scripts import inspect_sources as insp


def _make_pdf(path, text="Hello ParcelPilot policy text.", pages=1):
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def _make_workbook(path, sheets):
    """sheets: dict[name] -> list[list[value]] (first row is header)."""
    wb = openpyxl.Workbook()
    default_sheet = wb.active
    wb.remove(default_sheet)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    wb.save(path)


# --- PDF inspection ---------------------------------------------------------


def test_inspect_pdf_valid_reports_page_count_and_text_stats(tmp_path):
    pdf_path = tmp_path / "01_Support_Policy_v3_CURRENT.pdf"
    _make_pdf(pdf_path, text="Support policy clause one. Clause two follows.")

    result = insp.inspect_pdf(pdf_path)

    assert result["readable"] is True
    assert result["page_count"] == 1
    assert result["total_words"] > 0
    assert result["total_chars"] > 0
    assert result["pages"][0]["has_extractable_text"] is True
    assert result["error"] is None
    assert result["is_deprecated"] is False


def test_inspect_pdf_deprecated_flag_from_filename(tmp_path):
    pdf_path = tmp_path / "02_Support_Policy_v2_DEPRECATED.pdf"
    _make_pdf(pdf_path, text="Old policy text.")

    result = insp.inspect_pdf(pdf_path)

    assert result["is_deprecated"] is True


def test_inspect_pdf_multi_page(tmp_path):
    pdf_path = tmp_path / "multi.pdf"
    _make_pdf(pdf_path, text="Page text.", pages=3)

    result = insp.inspect_pdf(pdf_path)

    assert result["page_count"] == 3
    assert len(result["pages"]) == 3


def test_inspect_pdf_invalid_file_fails_gracefully(tmp_path):
    pdf_path = tmp_path / "not_a_real.pdf"
    pdf_path.write_bytes(b"this is not a pdf file, just garbage bytes")

    result = insp.inspect_pdf(pdf_path)

    assert result["readable"] is False
    assert result["error"] is not None
    assert result["page_count"] is None


# --- XLSX inspection ---------------------------------------------------------


def test_inspect_xlsx_reports_sheet_shapes_and_columns(tmp_path):
    xlsx_path = tmp_path / "data.xlsx"
    _make_workbook(
        xlsx_path,
        {
            "README": [
                ["Dataset snapshot", "2026-08-16 11:00 Asia/Kolkata"],
                ["Currency", "INR"],
            ],
            "accounts": [
                ["account_id", "account_name", "notes"],
                ["ACCT-001", "Acme", "first"],
                ["ACCT-002", "Beta", None],
            ],
        },
    )

    result = insp.inspect_xlsx(xlsx_path)

    assert result["readable"] is True
    assert result["sheet_names"] == ["README", "accounts"]
    assert result["sheets"]["accounts"]["row_count"] == 2
    assert result["sheets"]["accounts"]["column_count"] == 3
    col_names = [c["name"] for c in result["sheets"]["accounts"]["columns"]]
    assert col_names == ["account_id", "account_name", "notes"]


def test_inspect_xlsx_extracts_dataset_snapshot_from_readme(tmp_path):
    xlsx_path = tmp_path / "data.xlsx"
    _make_workbook(
        xlsx_path,
        {
            "README": [["Dataset snapshot", "2026-08-16 11:00 Asia/Kolkata"]],
            "accounts": [["account_id"], ["ACCT-001"]],
        },
    )

    result = insp.inspect_xlsx(xlsx_path)

    assert result["dataset_snapshot"] == "2026-08-16 11:00 Asia/Kolkata"


def test_inspect_xlsx_null_pattern_detection(tmp_path):
    xlsx_path = tmp_path / "data.xlsx"
    _make_workbook(
        xlsx_path,
        {
            "README": [["Dataset snapshot", "2026-01-01 00:00 UTC"]],
            "orders": [
                ["order_id", "pickup_actual_at"],
                ["ORD-1", None],
                ["ORD-2", "2026-08-16 09:35"],
                ["ORD-3", None],
            ],
        },
    )

    result = insp.inspect_xlsx(xlsx_path)

    pickup_col = next(
        c for c in result["sheets"]["orders"]["columns"] if c["name"] == "pickup_actual_at"
    )
    assert pickup_col["null_count"] == 2
    assert pickup_col["non_null_count"] == 1
    assert pickup_col["null_fraction"] == pytest.approx(2 / 3, abs=1e-3)


def test_inspect_xlsx_detects_foreign_key_relationship(tmp_path):
    xlsx_path = tmp_path / "data.xlsx"
    _make_workbook(
        xlsx_path,
        {
            "README": [["Dataset snapshot", "2026-01-01 00:00 UTC"]],
            "accounts": [["account_id"], ["ACCT-001"], ["ACCT-002"]],
            "orders": [["order_id", "account_id"], ["ORD-1", "ACCT-001"], ["ORD-2", "ACCT-002"]],
        },
    )

    result = insp.inspect_xlsx(xlsx_path)

    relationships = result["observed_relationships"]
    assert any(
        r["from_sheet"] == "orders"
        and r["from_column"] == "account_id"
        and r["likely_references_sheet"] == "accounts"
        for r in relationships
    )


def test_inspect_xlsx_invalid_file_fails_gracefully(tmp_path):
    xlsx_path = tmp_path / "not_a_real.xlsx"
    xlsx_path.write_bytes(b"this is not a real workbook")

    result = insp.inspect_xlsx(xlsx_path)

    assert result["readable"] is False
    assert result["error"] is not None
    assert result["sheets"] == {}


def test_sample_values_are_truncated(tmp_path):
    xlsx_path = tmp_path / "data.xlsx"
    long_text = "x" * 500
    _make_workbook(
        xlsx_path,
        {
            "README": [["Dataset snapshot", "2026-01-01 00:00 UTC"]],
            "tickets": [["ticket_id", "description"], ["TKT-1", long_text]],
        },
    )

    result = insp.inspect_xlsx(xlsx_path)

    desc_col = next(c for c in result["sheets"]["tickets"]["columns"] if c["name"] == "description")
    assert len(desc_col["sample_values"][0]) <= insp.MAX_SAMPLE_CHARS


# --- End-to-end report / CLI -------------------------------------------------


def _build_minimal_source_dir(tmp_path):
    for name in insp.PDF_FILES:
        _make_pdf(tmp_path / name, text=f"Text for {name}")
    _make_workbook(
        tmp_path / insp.XLSX_FILE,
        {
            "README": [["Dataset snapshot", "2026-08-16 11:00 Asia/Kolkata"]],
            "accounts": [["account_id", "account_name"], ["ACCT-001", "Acme"]],
        },
    )


def test_build_report_ok_for_complete_source_dir(tmp_path):
    _build_minimal_source_dir(tmp_path)

    report = insp.build_report(tmp_path)

    assert report["ok"] is True
    assert len(report["pdfs"]) == len(insp.PDF_FILES)
    assert report["xlsx"]["readable"] is True


def test_build_report_fails_when_pdf_missing(tmp_path):
    _build_minimal_source_dir(tmp_path)
    (tmp_path / insp.PDF_FILES[0]).unlink()

    report = insp.build_report(tmp_path)

    assert report["ok"] is False
    missing_entry = next(p for p in report["pdfs"] if p["name"] == insp.PDF_FILES[0])
    assert missing_entry["readable"] is False


def test_main_writes_report_and_returns_zero(tmp_path):
    _build_minimal_source_dir(tmp_path)
    output_path = tmp_path / "out" / "source_inspection.json"

    exit_code = insp.main(["--source-dir", str(tmp_path), "--output", str(output_path)])

    assert exit_code == 0
    assert output_path.is_file()
    data = json.loads(output_path.read_text(encoding="utf-8"))
    assert data["ok"] is True


def test_main_returns_nonzero_when_xlsx_missing(tmp_path):
    _build_minimal_source_dir(tmp_path)
    (tmp_path / insp.XLSX_FILE).unlink()
    output_path = tmp_path / "out.json"

    exit_code = insp.main(["--source-dir", str(tmp_path), "--output", str(output_path)])

    assert exit_code == 1


def test_inspect_does_not_modify_source_files(tmp_path):
    _build_minimal_source_dir(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}

    insp.build_report(tmp_path)

    after = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert before == after
