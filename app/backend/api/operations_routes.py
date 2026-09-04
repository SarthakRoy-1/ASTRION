"""Operations-intelligence endpoints.

Two read-only routes over the detection engine. They add no authorization
mechanism of their own: the caller is resolved by `authenticate`, the workspace
comes from their session, and the account scope is the same
`allowed_account_ids` every other tenant-scoped read uses. A signal is an
aggregation of records the caller can already read one at a time, so it must
be visible under exactly the same boundary — no wider, and no narrower.

Nothing here changes state. Acting on a signal goes through the ordinary
preparation tools and the confirmation gate; `recommended_next_step` is advice
a person reads, not an instruction anything executes.
"""

from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, Request

from app.backend.api.authentication import audit_denial, authenticate
from app.backend.api.dependencies import DbDep
from app.backend.api.ratelimit import client_address
from app.backend.auth.permissions import Permission
from app.backend.core.config import Settings
from app.backend.core.errors import AuthorizationError, NotFoundError
from app.backend.models.signals import Signal, SignalReport
from app.backend.operations.service import build_report, get_signal
from app.backend.services.audit import AuditEvent, hash_identifier, record_event

operations_router = APIRouter(prefix="/api/operations", tags=["operations"])

#: Cap on a listing. The view answers "what needs attention", and a page of
#: fifty answers nothing.
MAX_LIMIT = 50


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _authorize(request: Request, conn: sqlite3.Connection):
    """Resolve the caller and check the operations permission.

    Returns the caller so routes can scope from it. A caller without the
    permission gets 403 — they are a member of the workspace and simply lack
    this capability, which is the distinction the API draws everywhere else.
    """
    caller = authenticate(request, conn, _settings(request))
    if caller.is_demo:
        # The demo personas have no workspace and therefore no account scope to
        # derive signals from. Refusing plainly beats returning an empty list
        # that looks like "nothing is wrong".
        raise AuthorizationError(
            "Operations intelligence requires a real workspace session. "
            "Set AUTH_MODE=session to use it."
        )
    if not caller.has(Permission.READ_OPERATIONS):
        audit_denial(
            conn,
            caller,
            permission=Permission.READ_OPERATIONS,
            request_id=getattr(request.state, "request_id", None),
            client_ip=client_address(request),
            detail="operations signals",
        )
        raise AuthorizationError(
            "This action requires the 'operations.read' permission, which your "
            "role in this workspace does not grant."
        )
    return caller


def _signal_view(signal: Signal) -> dict:
    return {
        "signal_id": signal.signal_id,
        "signal_type": signal.signal_type.value,
        "severity": signal.severity.value,
        "priority_score": signal.priority_score,
        "priority_factors": [
            {"name": f.name, "points": f.points, "basis": f.basis}
            for f in signal.priority_factors
        ],
        "title": signal.title,
        "detail": signal.detail,
        "affected_account_ids": list(signal.affected_account_ids),
        "affected_account_count": signal.affected_account_count,
        "affected_ticket_count": signal.affected_ticket_count,
        "affected_order_count": signal.affected_order_count,
        "records": [
            {
                "kind": ref.kind.value,
                "record_id": ref.record_id,
                "account_id": ref.account_id,
                "label": ref.label,
            }
            for ref in signal.record_refs
        ],
        "documentation_chunk_ids": list(signal.evidence_chunk_ids),
        "first_observed_at": (
            signal.first_observed_at.isoformat() if signal.first_observed_at else None
        ),
        "last_observed_at": (
            signal.last_observed_at.isoformat() if signal.last_observed_at else None
        ),
        # Phase 2 trust vocabulary, reused rather than parallelled.
        "trust_status": signal.trust_status,
        "trust_reasons": list(signal.trust_reasons),
        "recommended_next_step": signal.recommended_next_step,
    }


def _report_view(report: SignalReport) -> dict:
    return {
        "signals": [_signal_view(s) for s in report.signals],
        "count": report.count,
        "highest_severity": (
            report.highest_severity.value if report.highest_severity else None
        ),
        # The clock every detector measured against. Never the wall clock —
        # a reader needs to know what "150 minutes overdue" is relative to.
        "reference_time": (
            report.reference_time.isoformat() if report.reference_time else None
        ),
        "scope_account_ids": list(report.scope_account_ids),
    }


@operations_router.get("/signals")
def list_signals(
    request: Request, conn: sqlite3.Connection = DbDep, limit: int = 20
) -> dict:
    """Every operational signal detected in the caller's workspace, ranked."""
    caller = _authorize(request, conn)
    started = time.perf_counter()

    report = build_report(
        conn,
        allowed_account_ids=caller.allowed_account_ids,
        limit=max(1, min(int(limit), MAX_LIMIT)),
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    record_event(
        conn,
        AuditEvent.OPERATIONS_SIGNALS_VIEWED,
        actor_user_id=caller.user_id,
        actor_role=caller.org_role,
        org_id=caller.org_id,
        request_id=getattr(request.state, "request_id", None),
        ip_hash=hash_identifier(client_address(request)),
        details={
            # Counts and labels, never ticket subjects or customer names: this
            # says what was surfaced, not what it said.
            "signal_count": report.count,
            "highest_severity": (
                report.highest_severity.value if report.highest_severity else None
            ),
            "signal_types": sorted({s.signal_type.value for s in report.signals}),
            "duration_ms": elapsed_ms,
        },
    )
    return _report_view(report)


@operations_router.get("/signals/{signal_id}")
def signal_detail(
    request: Request, signal_id: str, conn: sqlite3.Connection = DbDep
) -> dict:
    """One signal in full, re-derived under the caller's own scope.

    A signal from another workspace is reported as **absent**, not forbidden —
    the same refusal shape the record layer uses, so this endpoint cannot be
    used to discover that another tenant has a problem.
    """
    caller = _authorize(request, conn)

    signal = get_signal(
        conn, signal_id, allowed_account_ids=caller.allowed_account_ids
    )
    if signal is None:
        raise NotFoundError("That signal was not found.")

    record_event(
        conn,
        AuditEvent.OPERATIONS_SIGNAL_INSPECTED,
        actor_user_id=caller.user_id,
        actor_role=caller.org_role,
        org_id=caller.org_id,
        target_type="signal",
        target_id=signal.signal_id,
        request_id=getattr(request.state, "request_id", None),
        ip_hash=hash_identifier(client_address(request)),
        details={
            "signal_type": signal.signal_type.value,
            "severity": signal.severity.value,
            "priority_score": signal.priority_score,
            "affected_account_count": signal.affected_account_count,
            "affected_ticket_count": signal.affected_ticket_count,
            "affected_order_count": signal.affected_order_count,
            "trust_status": signal.trust_status,
        },
    )
    return _signal_view(signal)
