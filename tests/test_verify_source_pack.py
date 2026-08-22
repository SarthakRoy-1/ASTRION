import hashlib

import pytest

from scripts import verify_source_pack as vsp


def _write_expected_files(directory, content_prefix="content-"):
    for name in vsp.EXPECTED_FILES:
        (directory / name).write_bytes(f"{content_prefix}{name}".encode("utf-8"))


def test_all_expected_files_present_passes(tmp_path):
    _write_expected_files(tmp_path)

    report = vsp.verify(tmp_path)

    assert report["ok"] is True
    assert report["missing_files"] == []
    assert report["unexpected_files"] == []
    assert report["errors"] == []
    assert len(report["files"]) == len(vsp.EXPECTED_FILES)
    assert all(f["readable"] for f in report["files"])


def test_allowed_readme_is_not_flagged_as_unexpected(tmp_path):
    _write_expected_files(tmp_path)
    (tmp_path / "README.md").write_text("repo documentation, not a source file")

    report = vsp.verify(tmp_path)

    assert report["ok"] is True
    assert report["unexpected_files"] == []


def test_missing_file_is_detected(tmp_path):
    _write_expected_files(tmp_path)
    (tmp_path / "01_Support_Policy_v3_CURRENT.pdf").unlink()

    report = vsp.verify(tmp_path)

    assert report["ok"] is False
    assert report["missing_files"] == ["01_Support_Policy_v3_CURRENT.pdf"]
    assert any("missing expected file" in e for e in report["errors"])
    matching = [f for f in report["files"] if f["name"] == "01_Support_Policy_v3_CURRENT.pdf"]
    assert matching[0]["present"] is False
    assert matching[0]["readable"] is False


def test_multiple_missing_files_all_detected(tmp_path):
    _write_expected_files(tmp_path)
    (tmp_path / "01_Support_Policy_v3_CURRENT.pdf").unlink()
    (tmp_path / "ParcelPilot_Assessment_Data.xlsx").unlink()

    report = vsp.verify(tmp_path)

    assert report["ok"] is False
    assert set(report["missing_files"]) == {
        "01_Support_Policy_v3_CURRENT.pdf",
        "ParcelPilot_Assessment_Data.xlsx",
    }


def test_unexpected_file_is_detected(tmp_path):
    _write_expected_files(tmp_path)
    (tmp_path / "07_Unexpected_Extra_File.pdf").write_bytes(b"surprise")

    report = vsp.verify(tmp_path)

    assert report["ok"] is False
    assert report["unexpected_files"] == ["07_Unexpected_Extra_File.pdf"]
    assert any("unexpected file" in e for e in report["errors"])


def test_unexpected_file_does_not_mask_missing_file(tmp_path):
    _write_expected_files(tmp_path)
    (tmp_path / "02_Support_Policy_v2_DEPRECATED.pdf").unlink()
    (tmp_path / "not_part_of_the_pack.txt").write_bytes(b"stray file")

    report = vsp.verify(tmp_path)

    assert report["ok"] is False
    assert report["missing_files"] == ["02_Support_Policy_v2_DEPRECATED.pdf"]
    assert report["unexpected_files"] == ["not_part_of_the_pack.txt"]


def test_sha256_matches_hashlib_reference(tmp_path):
    _write_expected_files(tmp_path)

    report = vsp.verify(tmp_path)

    for entry in report["files"]:
        path = tmp_path / entry["name"]
        expected_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        assert entry["sha256"] == expected_hash
        assert len(entry["sha256"]) == 64


def test_hash_changes_when_content_changes(tmp_path):
    _write_expected_files(tmp_path, content_prefix="version-a-")
    report_a = vsp.verify(tmp_path)

    _write_expected_files(tmp_path, content_prefix="version-b-")
    report_b = vsp.verify(tmp_path)

    hashes_a = {f["name"]: f["sha256"] for f in report_a["files"]}
    hashes_b = {f["name"]: f["sha256"] for f in report_b["files"]}
    assert hashes_a != hashes_b


def test_unreadable_file_fails_verification(tmp_path, monkeypatch):
    _write_expected_files(tmp_path)

    def raising_sha256(path):
        if path.name == "03_Cancellation_and_Service_Credit_SOP_v4.pdf":
            raise OSError("simulated unreadable file")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    monkeypatch.setattr(vsp, "sha256_of", raising_sha256)

    report = vsp.verify(tmp_path)

    assert report["ok"] is False
    entry = next(f for f in report["files"] if f["name"] == "03_Cancellation_and_Service_Credit_SOP_v4.pdf")
    assert entry["readable"] is False
    assert entry["error"] is not None
    assert any("unreadable file" in e for e in report["errors"])


def test_verify_does_not_modify_source_files(tmp_path):
    _write_expected_files(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}

    vsp.verify(tmp_path)

    after = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert before == after


def test_verify_missing_source_directory(tmp_path):
    missing_dir = tmp_path / "does_not_exist"

    report = vsp.verify(missing_dir)

    assert report["ok"] is False
    assert report["errors"]


def test_main_returns_zero_on_success(tmp_path):
    _write_expected_files(tmp_path)

    exit_code = vsp.main(["--source-dir", str(tmp_path), "--json"])

    assert exit_code == 0


def test_main_returns_nonzero_on_failure(tmp_path):
    _write_expected_files(tmp_path)
    (tmp_path / "ParcelPilot_Assessment_Data.xlsx").unlink()

    exit_code = vsp.main(["--source-dir", str(tmp_path), "--json"])

    assert exit_code == 1


def test_verify_report_is_json_serializable(tmp_path):
    import json

    _write_expected_files(tmp_path)
    report = vsp.verify(tmp_path)

    serialized = json.dumps(report, sort_keys=True)
    assert json.loads(serialized) == report


def test_deterministic_across_runs(tmp_path):
    _write_expected_files(tmp_path)

    report_1 = vsp.verify(tmp_path)
    report_2 = vsp.verify(tmp_path)

    assert report_1 == report_2
