"""Phase 6: the contract the frontend is built against.

The UI does not hand-write the backend's types or invent example payloads. It
generates its TypeScript from `app/frontend/openapi.json` and renders fixtures
recorded from real API responses. Both are committed so the frontend builds and
tests standalone — which means both can go stale.

These tests are the thing that stops that. A field renamed in
`app/backend/api/schemas.py`, or a response whose shape or substance changed,
fails here rather than being discovered as `undefined` in a browser.

Neither test needs an API key: everything runs on the deterministic provider.
"""

import json

from scripts import export_openapi, export_ui_fixtures


def test_committed_openapi_schema_matches_the_application():
    """The schema the frontend generates its types from is current."""
    expected = export_openapi.render(export_openapi.build_schema())
    actual = export_openapi.DEFAULT_OUTPUT.read_text(encoding="utf-8")

    assert actual == expected, (
        "app/frontend/openapi.json is stale. Regenerate it:\n"
        "    python scripts/export_openapi.py\n"
        "    cd app/frontend && npm run generate:api"
    )


def test_recorded_ui_fixtures_match_real_api_responses(full_db):
    """Every fixture the UI tests render is still what the API returns.

    Recorded against this test's private database copy, so the check costs one
    extra pass over the already-ingested data rather than a fresh ingestion —
    and so it can never touch `data/processed/astrion.db`.
    """
    recorded = export_ui_fixtures.record(full_db)
    stale: list[str] = []

    for name, body in recorded.items():
        path = export_ui_fixtures.OUTPUT_DIR / f"{name}.json"
        expected = export_ui_fixtures.render(name, body)
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current != expected:
            stale.append(name)

    assert not stale, (
        f"Frontend fixtures are stale: {', '.join(sorted(stale))}.\n"
        f"Re-record them: python scripts/export_ui_fixtures.py"
    )


def test_every_recorded_fixture_is_valid_json_on_disk():
    """A malformed fixture would fail the UI suite with an unrelated error."""
    files = sorted(export_ui_fixtures.OUTPUT_DIR.glob("*.json"))

    assert files, "no UI fixtures recorded; run scripts/export_ui_fixtures.py"
    for path in files:
        json.loads(path.read_text(encoding="utf-8"))


def test_the_pending_action_fixture_really_is_pending():
    """The UI's confirmation tests are meaningless if it is not.

    A fixture that silently became `executed` would let the confirmation flow's
    tests pass while rendering a state the UI never actually gates on.
    """
    path = export_ui_fixtures.OUTPUT_DIR / "chat-pending-action.json"
    body = json.loads(path.read_text(encoding="utf-8"))

    assert body["action_status"] == "pending_confirmation"
    assert body["proposed_action"] is not None
    assert body["proposed_action"]["parameter_fingerprint"]
