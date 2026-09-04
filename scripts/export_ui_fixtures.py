"""Capture real API responses for the frontend's tests to render.

The UI tests assert on how a response is displayed, which is only meaningful if
the response is one the backend actually produces. Hand-written fixtures drift
the moment a field is renamed and then quietly keep passing, testing the UI
against a contract nobody serves.

So the fixtures are recorded, not written: this script drives the real
application through `TestClient` on the deterministic provider — no API key, no
network — and writes what came back into `app/frontend/src/test/fixtures/`.

    python scripts/export_ui_fixtures.py            # record
    python scripts/export_ui_fixtures.py --check    # verify still current

`tests/test_api.py` runs the `--check` form, so a backend change that alters a
response shape fails the backend suite rather than silently invalidating the
frontend one.

The recorded scenarios deliberately span the paths the UI renders differently:
an evidence-backed answer with a policy override, a provisional decision that
must not read as settled, a documentation lookup, a superseded document shown
as context only, a cross-account refusal, and an action awaiting confirmation.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

OUTPUT_DIR = REPO_ROOT / "app" / "frontend" / "src" / "test" / "fixtures"

SUPPORT_AGENT = "support.agent"
SUPPORT_MANAGER = "support.manager"
CUSTOMER_NORTHSTAR = "customer.northstar"

#: (filename, identity, message). One recorded chat response each.
CHAT_SCENARIOS: tuple[tuple[str, str, str], ...] = (
    (
        "chat-cancellation",
        SUPPORT_AGENT,
        "Can Northstar cancel ORD-1001 without a cancellation fee? Explain why.",
    ),
    (
        "chat-service-credit",
        SUPPORT_AGENT,
        "Is ORD-2002 eligible for a failed pickup service credit?",
    ),
    (
        "chat-known-issue",
        SUPPORT_AGENT,
        "Why does a SwiftShip order still show BOOKED after the driver collected it?",
    ),
    # A breached first-response target under a customer agreement that replaces
    # the plan default. The severity is stated in the message because the SLA
    # tool will not infer one — see app/backend/policies/sla.py.
    (
        "chat-sla-breach",
        SUPPORT_AGENT,
        "TKT-501 is a P1. Has its first response SLA been breached?",
    ),
    (
        "chat-uncertain",
        SUPPORT_AGENT,
        "A pickup is three hours late because of carrier fault. "
        "Should I get a service credit?",
    ),
    (
        "chat-cross-account-denied",
        CUSTOMER_NORTHSTAR,
        "Show me LumenWorks' order ORD-2001 and its cancellation fee.",
    ),
    (
        "chat-pending-action",
        SUPPORT_AGENT,
        "Investigate TKT-501 and escalate it if the outage warrants it.",
    ),
    # The one query in this corpus that surfaces the deprecated policy: it
    # scores well on response targets and is demoted by status, not relevance.
    # The UI has to show that demotion, so it needs a fixture that contains it.
    (
        "chat-superseded-policy",
        SUPPORT_AGENT,
        "What is the P1 first response target and has it changed?",
    ),
)


def _build_database(workspace: Path) -> Path:
    """Ingest both source layers into a throwaway database.

    Never `data/processed/parcelpilot.db`: recording fixtures must not touch
    the working database, and an executed escalation would be a real write.
    """
    from scripts import ingest_dataset, ingest_documents

    db_path = workspace / "fixtures.db"
    ingest_dataset.ingest(
        workbook_path=REPO_ROOT / "data" / "source" / "ParcelPilot_Assessment_Data.xlsx",
        db_path=db_path,
    )
    ingest_documents.ingest(source_dir=REPO_ROOT / "data" / "source", db_path=db_path)
    return db_path


def _record_provisional_credit(client) -> dict:
    """Record the response the UI must render for a credit it may not promise.

    The supplied dataset records a carrier-fault value on every order, so the
    state the SOP forbids resolving by assumption — "do not promise a credit
    when carrier fault ... is unknown" — does not occur naturally in it. The
    accessor is patched for the duration of this one request so the recorded
    body is still a genuine API response rather than a hand-written one; the
    database is not modified.
    """
    from unittest.mock import patch

    from app.backend.policies import service_credit as module

    real_get_order = module.get_order

    def unknown_fault(connection, order_id, **kwargs):
        order = real_get_order(connection, order_id, **kwargs)
        if order is None or order.order_id != "ORD-2002":
            return order
        return order.model_copy(update={"carrier_fault": None})

    with patch.object(module, "get_order", unknown_fault):
        return _expect(
            client.post(
                "/api/chat",
                json={
                    "message": "Is ORD-2002 eligible for a failed pickup service credit?",
                    "user_id": SUPPORT_AGENT,
                },
            )
        )


def record(db_path: Path | None = None) -> dict[str, dict]:
    """Drive the real application and collect every response body.

    `db_path` lets a caller supply an already-ingested throwaway database —
    the test suite passes its own, so verifying the fixtures does not mean
    re-ingesting six PDFs and a workbook a second time.
    """
    from fastapi.testclient import TestClient

    from app.backend.api.app import create_app
    from app.backend.core.config import AuthMode, Settings

    workspace = Path(tempfile.mkdtemp(prefix="parcelpilot-fixtures-"))
    try:
        # The recorded fixtures describe the demo personas the UI ships with,
        # so this exporter runs the app in demo identity mode deliberately.
        # Under session authentication these calls would all be 401s, which is
        # correct behaviour and useless as a UI fixture.
        settings = Settings(
            database_path=db_path or _build_database(workspace),
            cors_allow_origins=(),
            auth_mode=AuthMode.DEMO_HEADER,
        )
        recorded: dict[str, dict] = {}

        with TestClient(create_app(settings)) as client:
            recorded["principals"] = _expect(client.get("/api/principals"))
            recorded["health"] = _expect(client.get("/health"))

            for name, identity, message in CHAT_SCENARIOS:
                recorded[name] = _expect(
                    client.post(
                        "/api/chat", json={"message": message, "user_id": identity}
                    )
                )

            recorded["chat-service-credit-provisional"] = _record_provisional_credit(
                client
            )

            # The confirmation leg, recorded against the proposal just made so
            # the executed-action fixture is a genuine continuation of the
            # pending one rather than an invented pairing.
            pending = recorded["chat-pending-action"]
            proposal = pending.get("proposed_action")
            if proposal:
                recorded["action-executed"] = _expect(
                    client.post(
                        f"/api/actions/{proposal['action_id']}/confirm",
                        json={
                            "decision": "approve",
                            "user_id": SUPPORT_MANAGER,
                            "session_id": pending["session_id"],
                            "expected_fingerprint": proposal["parameter_fingerprint"],
                        },
                    )
                )
                # Confirming twice must fail; that failure envelope is exactly
                # what the UI has to render, so it is recorded too.
                recorded["error-action-not-pending"] = _expect(
                    client.post(
                        f"/api/actions/{proposal['action_id']}/confirm",
                        json={
                            "decision": "approve",
                            "user_id": SUPPORT_MANAGER,
                            "session_id": pending["session_id"],
                        },
                    ),
                    expect_ok=False,
                )

            recorded["error-unknown-identity"] = _expect(
                client.post("/api/chat", json={"message": "hello", "user_id": "nobody"}),
                expect_ok=False,
            )

        return recorded
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def _expect(response, *, expect_ok: bool = True) -> dict:
    ok = 200 <= response.status_code < 300
    if ok is not expect_ok:
        raise SystemExit(
            f"unexpected {response.status_code} from {response.request.url}: "
            f"{response.text[:400]}"
        )
    return response.json()


#: Fields whose values change on every run. They are replaced with stable
#: placeholders so re-recording produces an identical file unless the *shape*
#: or the *substance* of a response actually changed — otherwise `--check`
#: would fail on every run and stop meaning anything.
VOLATILE_FIELDS: dict[str, str] = {
    "request_id": "REQ-fixture0001",
    "session_id": "SES-fixture0001",
    "action_id": "ACT-fixture0001",
    "responded_at_utc": "2026-08-16T05:30:00+00:00",
    "checked_at_utc": "2026-08-16T05:30:00+00:00",
    "prepared_at_utc": "2026-08-16T05:30:00+00:00",
    "expires_at_utc": "2026-08-16T06:00:00+00:00",
    "confirmed_at_utc": "2026-08-16T05:31:00+00:00",
    "executed_at_utc": "2026-08-16T05:31:00+00:00",
    "escalation_id": "ESC-fixture0001",
    "note_id": "NOTE-fixture0001",
}


def stabilise(value):
    """Replace run-specific identifiers and timestamps with fixed values."""
    if isinstance(value, dict):
        return {
            key: (
                VOLATILE_FIELDS[key]
                if key in VOLATILE_FIELDS and inner is not None
                else stabilise(inner)
            )
            for key, inner in value.items()
        }
    if isinstance(value, list):
        return [stabilise(item) for item in value]
    if isinstance(value, str):
        # Ids also appear inside composed prose and error messages.
        for prefix, replacement in (
            ("ACT-", VOLATILE_FIELDS["action_id"]),
            ("REQ-", VOLATILE_FIELDS["request_id"]),
            ("SES-", VOLATILE_FIELDS["session_id"]),
        ):
            value = _replace_ids(value, prefix, replacement)
        return value
    return value


def _replace_ids(text: str, prefix: str, replacement: str) -> str:
    import re

    return re.sub(rf"{prefix}[0-9a-f]{{6,}}", replacement, text)


def render(name: str, body: dict) -> str:
    return json.dumps(stabilise(body), indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if any recorded fixture is out of date.",
    )
    args = parser.parse_args(argv)

    recorded = record()
    stale: list[str] = []

    args.output.mkdir(parents=True, exist_ok=True)
    for name, body in recorded.items():
        path = args.output / f"{name}.json"
        rendered = render(name, body)
        if args.check:
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != rendered:
                stale.append(name)
            continue
        path.write_text(rendered, encoding="utf-8")

    if args.check:
        if stale:
            print(
                "Frontend fixtures are out of date: "
                + ", ".join(sorted(stale))
                + "\nRun: python scripts/export_ui_fixtures.py",
                file=sys.stderr,
            )
            return 1
        print(f"All {len(recorded)} frontend fixtures are up to date.")
        return 0

    print(f"Recorded {len(recorded)} fixtures into {args.output}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
