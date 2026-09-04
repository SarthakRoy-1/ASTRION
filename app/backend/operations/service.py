"""The operations-intelligence entry point.

One function assembles a workspace's signal report — detect, rank, package —
so every caller (the HTTP route, the agent tool, the evaluation suite) goes
through the same path and sees the same result. A second implementation would
drift the moment either changed.

Scope is a required argument rather than an optional one with a permissive
default. `detect_signals` accepts `None` for unrestricted access because the
policy engine and its tests legitimately need it, but nothing reachable from a
request should ever pass it, so this layer makes the caller state the boundary
explicitly.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection

from app.backend.models.signals import Signal, SignalReport
from app.backend.operations.detection import detect_signals
from app.backend.operations.ranking import rank


def build_report(
    conn: sqlite3.Connection,
    *,
    allowed_account_ids: Collection[str] | None,
    limit: int | None = None,
) -> SignalReport:
    """Detect and rank every signal visible under one tenant scope.

    `limit` trims the *ranked* list, so a caller asking for the top three gets
    the three most urgent rather than the first three detected.
    """
    signals, reference_time = detect_signals(
        conn, allowed_account_ids=allowed_account_ids
    )
    ordered = rank(signals)
    if limit is not None:
        ordered = ordered[: max(0, limit)]

    return SignalReport(
        signals=ordered,
        reference_time=reference_time,
        scope_account_ids=(
            sorted(allowed_account_ids) if allowed_account_ids is not None else []
        ),
    )


def get_signal(
    conn: sqlite3.Connection,
    signal_id: str,
    *,
    allowed_account_ids: Collection[str] | None,
) -> Signal | None:
    """One signal by id, or None.

    Deliberately re-derives the whole report rather than looking the signal up
    in a store. Signals are a *view* over operational data, not persisted rows,
    so re-deriving under the caller's own scope is what guarantees they can only
    ever retrieve a signal their workspace can actually produce. Handing back a
    cached signal computed under someone else's scope is the exact shape of a
    cross-tenant leak, and this design makes it unrepresentable.

    None means "not found within your scope", indistinguishable from "does not
    exist" — the same refusal shape the record layer uses, for the same reason.
    """
    report = build_report(conn, allowed_account_ids=allowed_account_ids)
    for signal in report.signals:
        if signal.signal_id == signal_id:
            return signal
    return None
