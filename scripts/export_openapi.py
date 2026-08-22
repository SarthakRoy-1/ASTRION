"""Export the FastAPI OpenAPI schema for the frontend to generate types from.

The frontend's TypeScript types are *generated* from this file
(`npm run generate:api`), never hand-written. That is what keeps one contract
rather than two: a field renamed in `app/backend/api/schemas.py` becomes a
TypeScript compile error in the UI instead of a runtime `undefined`.

The schema is written into `app/frontend/` and committed, so the frontend
builds and tests without a running backend. `tests/test_api.py` asserts the
committed copy still matches what the application generates, so the two cannot
drift silently.

    python scripts/export_openapi.py            # write the schema
    python scripts/export_openapi.py --check    # verify it is up to date
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_OUTPUT = REPO_ROOT / "app" / "frontend" / "openapi.json"


def build_schema() -> dict:
    """Generate the schema without starting a server or touching the database."""
    from app.backend.api.app import create_app
    from app.backend.core.config import Settings

    # Explicit default settings rather than the ambient environment: the schema
    # describes the API's shape, which must not vary with whoever exported it.
    return create_app(Settings()).openapi()


def render(schema: dict) -> str:
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the file on disk is out of date, and write nothing.",
    )
    args = parser.parse_args(argv)

    rendered = render(build_schema())

    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if current != rendered:
            print(
                f"{args.output} is out of date. Run:\n"
                f"    python scripts/export_openapi.py\n"
                f"    cd app/frontend && npm run generate:api",
                file=sys.stderr,
            )
            return 1
        print(f"{args.output} is up to date.")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"Wrote {args.output} ({len(rendered):,} bytes).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
