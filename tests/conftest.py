import copy
import sys
from pathlib import Path

import openpyxl
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# A real filename from the supplied source pack, reused here only because
# scripts/ingest_dataset.py validates accounts.contract_file against the
# actual delivered PDFs — a synthetic filename would correctly fail that
# check, so "happy path" fixtures borrow a real name.
SAMPLE_CONTRACT_FILE = "05_Northstar_Logistics_Enterprise_Agreement.pdf"

DEFAULT_SHEETS = {
    "README": [
        ["Dataset snapshot", "2026-01-10 09:00 Asia/Kolkata"],
        ["Currency", "INR"],
        ["Notes", "Synthetic test dataset."],
        ["Important", "Historical resolutions may be wrong."],
    ],
    "accounts": [
        ["account_id", "account_name", "plan", "status", "csm", "contract_file", "premium_support", "notes"],
        ["ACCT-A", "Alpha Co", "Enterprise", "active", "Csm One", SAMPLE_CONTRACT_FILE, True, "Has a contract."],
        ["ACCT-B", "Beta Co", "Standard", "active", "Csm Two", None, False, "No contract on file."],
    ],
    "orders": [
        [
            "order_id", "account_id", "carrier", "status", "booked_at",
            "pickup_window_start", "pickup_window_end", "pickup_actual_at",
            "shipment_fee_inr", "carrier_fault", "customer_fault",
            "cancellation_requested_at", "notes",
        ],
        [
            "ORD-1", "ACCT-A", "SwiftShip", "BOOKED", "2026-01-10 09:00",
            "2026-01-10 10:00", "2026-01-10 11:00", None,
            1000.0, False, False, "2026-01-10 09:30", "Cancel requested before pickup.",
        ],
        [
            "ORD-2", "ACCT-B", "RoadRunner", "PICKED_UP", "2026-01-09 08:00",
            "2026-01-09 09:00", "2026-01-09 10:00", "2026-01-09 09:15",
            500.0, True, False, None, "Picked up on time.",
        ],
    ],
    "tickets": [
        [
            "ticket_id", "account_id", "created_at", "status", "subject", "description",
            "channel", "assigned_to", "last_customer_message_at", "historical_resolution",
        ],
        [
            "TKT-1", "ACCT-A", "2026-01-10 08:00", "open", "Question", "A question.",
            "email", "Agent A", "2026-01-10 08:05", None,
        ],
        [
            "TKT-2", "ACCT-B", "2025-12-01 10:00", "closed", "Old issue", "An old issue.",
            "chat", "Agent B", "2025-12-01 10:10", "Told customer X.",
        ],
    ],
}


def default_sheets() -> dict:
    """A deep copy of a valid, minimal-but-representative workbook's sheets,
    safe for a test to mutate without affecting other tests."""
    return copy.deepcopy(DEFAULT_SHEETS)


def write_workbook(path: Path, sheets: dict) -> None:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    wb.save(path)


@pytest.fixture
def sample_workbook_path(tmp_path) -> Path:
    path = tmp_path / "sample.xlsx"
    write_workbook(path, default_sheets())
    return path


# ---------------------------------------------------------------------------
# Phase 3: document / evidence layer
# ---------------------------------------------------------------------------

SOURCE_DIR = REPO_ROOT / "data" / "source"

NORTHSTAR_PDF = "05_Northstar_Logistics_Enterprise_Agreement.pdf"
LUMENWORKS_PDF = "06_LumenWorks_Service_Agreement.pdf"
CURRENT_POLICY_PDF = "01_Support_Policy_v3_CURRENT.pdf"
DEPRECATED_POLICY_PDF = "02_Support_Policy_v2_DEPRECATED.pdf"
SOP_PDF = "03_Cancellation_and_Service_Credit_SOP_v4.pdf"
PRODUCT_GUIDE_PDF = "04_Product_Operations_Guide_and_Known_Issues.pdf"

NORTHSTAR_DOC_ID = "05_northstar_logistics_enterprise_agreement"
LUMENWORKS_DOC_ID = "06_lumenworks_service_agreement"
CURRENT_POLICY_DOC_ID = "01_support_policy_v3_current"
DEPRECATED_POLICY_DOC_ID = "02_support_policy_v2_deprecated"
SOP_DOC_ID = "03_cancellation_and_service_credit_sop_v4"


def write_pdf(path: Path, pages: list[list[tuple[str, float, bool]]]) -> None:
    """Write a synthetic PDF from (text, font_size, bold) lines per page.

    Mirrors the typographic convention of the real supplied pack (24pt bold
    title, 18pt bold section, 14pt bold sub-section, 11pt body) so extraction
    is exercised the same way real documents exercise it. Used for malformed
    and edge-case inputs the real pack cannot provide.
    """
    import pymupdf

    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page()
        y = 72.0
        for text, size, bold in lines:
            page.insert_text(
                (72, y), text, fontsize=size, fontname="hebo" if bold else "helv"
            )
            y += size * 1.9
    doc.save(str(path))
    doc.close()


def valid_agreement_pages(
    *,
    title: str = "ParcelPilot - Testco Service Agreement",
    account_line: str | None = "Account: ACCT-999",
    status_line: str | None = "Status: ACTIVE",
) -> list[list[tuple[str, float, bool]]]:
    """A structurally valid one-page customer agreement. Individual metadata
    lines can be dropped to build malformed variants."""
    preamble: list[tuple[str, float, bool]] = [(title, 24, True)]
    if account_line is not None:
        preamble.append((account_line, 11, True))
    preamble.append(("Customer: Testco", 11, True))
    if status_line is not None:
        preamble.append((status_line, 11, True))
    return [
        preamble
        + [
            ("1. Cancellation terms", 18, True),
            ("Testco may cancel any BOOKED shipment with no fee.", 11, False),
            ("2. Service credits", 18, True),
            ("A fixed INR 700 credit applies to failed pickups.", 11, False),
        ]
    ]


@pytest.fixture(scope="session")
def documents_db(tmp_path_factory) -> Path:
    """A throwaway database with the six real PDFs ingested.

    Session-scoped because ingestion is deterministic and read-only from the
    tests' point of view. It writes to a pytest temp directory, never to
    data/processed/parcelpilot.db — the real assessment database is never
    touched by the suite.
    """
    from scripts import ingest_documents

    db_path = tmp_path_factory.mktemp("documents") / "documents.db"
    ingest_documents.ingest(source_dir=SOURCE_DIR, db_path=db_path)
    return db_path


@pytest.fixture
def doc_conn(documents_db):
    """Connection to the ingested-real-PDFs database."""
    from app.backend.services.database import get_connection

    conn = get_connection(documents_db)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Phase 4: agent, tools, policy, actions
# ---------------------------------------------------------------------------

WORKBOOK = SOURCE_DIR / "ParcelPilot_Assessment_Data.xlsx"

NORTHSTAR_ACCOUNT = "ACCT-001"
LUMENWORKS_ACCOUNT = "ACCT-002"
BEACON_ACCOUNT = "ACCT-003"


@pytest.fixture(scope="session")
def _full_db_template(tmp_path_factory) -> Path:
    """Both source layers ingested once: the workbook and the six PDFs.

    Built once per session and copied per test, so tests that execute actions
    cannot see each other's writes. Never touches
    data/processed/parcelpilot.db.
    """
    from scripts import ingest_dataset, ingest_documents

    db_path = tmp_path_factory.mktemp("full") / "template.db"
    ingest_dataset.ingest(workbook_path=WORKBOOK, db_path=db_path)
    ingest_documents.ingest(source_dir=SOURCE_DIR, db_path=db_path)
    return db_path


@pytest.fixture
def full_db(_full_db_template, tmp_path) -> Path:
    """A private copy of the fully ingested database for one test."""
    import shutil

    db_path = tmp_path / "parcelpilot.db"
    shutil.copy(_full_db_template, db_path)
    return db_path


@pytest.fixture
def conn(full_db):
    """Connection to a private, fully ingested database."""
    from app.backend.services.database import get_connection, initialize_schema

    connection = get_connection(full_db)
    initialize_schema(connection)
    yield connection
    connection.close()


@pytest.fixture
def agent_context():
    from app.backend.models.agent import AgentContext, Role

    return AgentContext(user_id="agent.test", role=Role.SUPPORT_AGENT)


@pytest.fixture
def manager_context():
    from app.backend.models.agent import AgentContext, Role

    return AgentContext(user_id="manager.test", role=Role.SUPPORT_MANAGER)


@pytest.fixture
def readonly_context():
    from app.backend.models.agent import AgentContext, Role

    return AgentContext(user_id="viewer.test", role=Role.READ_ONLY)


@pytest.fixture
def northstar_context():
    """A caller authorized for Northstar's account only."""
    from app.backend.models.agent import AgentContext, Role

    return AgentContext(
        user_id="ns.agent",
        role=Role.SUPPORT_AGENT,
        allowed_account_ids=frozenset({NORTHSTAR_ACCOUNT}),
    )


@pytest.fixture
def orchestrator(conn):
    from app.backend.agent.orchestrator import AgentOrchestrator

    return AgentOrchestrator(conn)


# ---------------------------------------------------------------------------
# Phase 5: FastAPI surface
# ---------------------------------------------------------------------------


@pytest.fixture
def api_settings(full_db):
    """Settings pointing at this test's private database copy.

    Constructed directly rather than through `load_settings`, so the suite is
    unaffected by whatever is in the developer's real environment or `.env` —
    and so no test can accidentally depend on an API key being present.
    """
    from app.backend.core.config import AuthMode, Settings

    # `auth_mode` is stated explicitly rather than inherited. These tests
    # exercise the agent, the policy engine and the action state machine under
    # a named demo persona, which is what `DEMO_HEADER` exists for — and saying
    # so here keeps the *default* (`AuthMode.SESSION`) genuinely secure instead
    # of being loosened to suit the suite. The session-authenticated path and
    # the tenant-isolation attacks against it are covered separately, in
    # tests/test_security_auth.py and tests/test_security_adversarial.py.
    return Settings(
        database_path=full_db,
        cors_allow_origins=(),
        auth_mode=AuthMode.DEMO_HEADER,
    )


@pytest.fixture
def api_app(api_settings):
    from app.backend.api.app import create_app

    return create_app(api_settings)


@pytest.fixture
def client(api_app):
    """A client over the deterministic provider — no API key, no network."""
    from fastapi.testclient import TestClient

    with TestClient(api_app) as test_client:
        yield test_client


@pytest.fixture
def raw_client(api_app):
    """A client that returns the 500 envelope instead of re-raising.

    `TestClient` re-raises unhandled server exceptions by default, which would
    hide the very handler under test.
    """
    from fastapi.testclient import TestClient

    with TestClient(api_app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def unknown_carrier_fault(monkeypatch):
    """Blank out one order's recorded carrier fault.

    The supplied dataset records a fault value for every order, so the state
    the SOP forbids resolving by assumption — "do not promise a credit when
    carrier fault ... is unknown" — has to be constructed to be tested. Patches
    the policy module's own record accessor, leaving the database untouched so
    no other test observes the change.
    """

    def apply(order_id: str) -> None:
        from app.backend.policies import service_credit as module

        real_get_order = module.get_order

        def patched(connection, requested_id, **kwargs):
            order = real_get_order(connection, requested_id, **kwargs)
            if order is None or order.order_id != order_id:
                return order
            return order.model_copy(update={"carrier_fault": None})

        monkeypatch.setattr(module, "get_order", patched)

    return apply


SUPPORT_AGENT = "support.agent"
SUPPORT_MANAGER = "support.manager"
SUPPORT_READONLY = "support.readonly"
CUSTOMER_NORTHSTAR = "customer.northstar"
CUSTOMER_LUMENWORKS = "customer.lumenworks"


def post_chat(client, message: str, user_id: str = SUPPORT_AGENT, **extra):
    """Send one chat request and return the parsed body plus the response."""
    payload = {"message": message, "user_id": user_id, **extra}
    return client.post("/api/chat", json=payload)
