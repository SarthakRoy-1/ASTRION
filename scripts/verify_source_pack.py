"""Verify the assessment source pack in data/source/.

Checks that exactly the expected assessment files are present, readable, and
hashes them with SHA-256. Never modifies anything under data/source/.

Usage:
    python scripts/verify_source_pack.py [--json]

Exit codes:
    0  verification passed
    1  verification failed (missing file, unexpected file, or unreadable file)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = REPO_ROOT / "data" / "source"

# Filenames are load-bearing: _CURRENT / _DEPRECATED feed the source-authority
# precedence chain documented in docs/architecture.md.
EXPECTED_FILES = (
    "01_Support_Policy_v3_CURRENT.pdf",
    "02_Support_Policy_v2_DEPRECATED.pdf",
    "03_Cancellation_and_Service_Credit_SOP_v4.pdf",
    "04_Product_Operations_Guide_and_Known_Issues.pdf",
    "05_Northstar_Logistics_Enterprise_Agreement.pdf",
    "06_LumenWorks_Service_Agreement.pdf",
    "ParcelPilot_Assessment_Data.xlsx",
)

# Allowed repository documentation living alongside the source pack. Not part
# of the assessment source and excluded from the expected/unexpected checks.
ALLOWED_NON_SOURCE_FILES = frozenset({"README.md"})

CHUNK_SIZE = 1024 * 1024


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(source_dir: Path) -> dict:
    """Return a deterministic verification report. Never writes to source_dir."""
    result: dict = {
        "source_dir": str(source_dir),
        "ok": True,
        "errors": [],
        "files": [],
        "missing_files": [],
        "unexpected_files": [],
    }

    if not source_dir.is_dir():
        result["ok"] = False
        result["errors"].append(f"source directory does not exist: {source_dir}")
        return result

    actual_names = {
        p.name for p in source_dir.iterdir() if p.is_file()
    }

    missing = sorted(name for name in EXPECTED_FILES if name not in actual_names)
    unexpected = sorted(
        name
        for name in actual_names
        if name not in EXPECTED_FILES and name not in ALLOWED_NON_SOURCE_FILES
    )

    result["missing_files"] = missing
    result["unexpected_files"] = unexpected

    if missing:
        result["ok"] = False
        for name in missing:
            result["errors"].append(f"missing expected file: {name}")

    if unexpected:
        result["ok"] = False
        for name in unexpected:
            result["errors"].append(f"unexpected file in source pack: {name}")

    # Hash and readability-check every expected file that is actually present,
    # in a fixed order, regardless of what else went wrong above.
    for name in EXPECTED_FILES:
        path = source_dir / name
        entry = {
            "name": name,
            "present": path.is_file(),
            "readable": False,
            "size_bytes": None,
            "sha256": None,
            "error": None,
        }
        if entry["present"]:
            try:
                entry["size_bytes"] = path.stat().st_size
                entry["sha256"] = sha256_of(path)
                entry["readable"] = True
            except OSError as exc:
                entry["error"] = str(exc)
                result["ok"] = False
                result["errors"].append(f"unreadable file: {name} ({exc})")
        result["files"].append(entry)

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=SOURCE_DIR,
        help="Directory containing the assessment source pack (default: data/source)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print only the JSON report (no human-readable summary)",
    )
    args = parser.parse_args(argv)

    report = verify(args.source_dir)
    report_json = json.dumps(report, indent=2, sort_keys=True)

    if args.json:
        print(report_json)
    else:
        for entry in report["files"]:
            status = "OK" if entry["readable"] else "FAIL"
            sha = entry["sha256"] or "-"
            print(f"[{status}] {entry['name']:55s} sha256={sha}")
        if report["missing_files"]:
            print(f"Missing: {', '.join(report['missing_files'])}")
        if report["unexpected_files"]:
            print(f"Unexpected: {', '.join(report['unexpected_files'])}")
        print()
        print("VERIFICATION PASSED" if report["ok"] else "VERIFICATION FAILED")
        print()
        print(report_json)

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
