"""Shared foundations for the deterministic policy engine (Phase 4).

Two concerns live here because both calculators need them and neither should
re-implement them:

1. **The reference clock.** Time-based questions are evaluated against the
   dataset's own snapshot time, read from `dataset_metadata`, never against
   `datetime.now()`. The supplied data is a fixed snapshot; judging a
   30-minute cancellation window against wall-clock time would change the
   answer every day the assessment is re-run.

2. **Evidence gathering.** Both calculators need the clauses that apply *to
   one account*, layered by authority. This module is the only place that
   assembles them, so the scoping rule below cannot be forgotten in one
   calculator and remembered in the other.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection
from datetime import datetime
from decimal import Decimal

from app.backend.models.documents import AuthorityDecision, Evidence, Topic
from app.backend.models.policy import PolicyEvaluationContext
from app.backend.retrieval.authority import resolve_authority
from app.backend.services.documents import get_evidence_by_topic
from app.backend.services.records import get_dataset_metadata


class PolicyLookupError(Exception):
    """A record the calculation needs is missing or out of the caller's scope.

    Deliberately does not distinguish the two: telling an out-of-scope caller
    that a record *exists* but is forbidden leaks its existence, which is the
    same reason the Phase 2/3 repositories return None for both.
    """


class PolicyDataError(Exception):
    """Structured data the calculation depends on is internally inconsistent."""


def load_evaluation_context(conn: sqlite3.Connection) -> PolicyEvaluationContext:
    """Build the reference clock from the ingested dataset metadata."""
    metadata = get_dataset_metadata(conn)
    if metadata is None:
        raise PolicyDataError(
            "dataset_metadata is empty — run scripts/ingest_dataset.py before "
            "evaluating time-based policy"
        )
    return PolicyEvaluationContext(
        reference_time=metadata.dataset_snapshot_at,
        reference_time_source=(
            f"dataset snapshot {metadata.dataset_snapshot_raw} "
            f"(from {metadata.source_workbook_filename})"
        ),
        currency=metadata.currency or "INR",
    )


def gather_policy_evidence(
    conn: sqlite3.Connection,
    *,
    topic: Topic,
    account_id: str,
    allowed_account_ids: Collection[str] | None,
) -> tuple[list[Evidence], AuthorityDecision]:
    """Collect the clauses governing `topic` for exactly one account.

    `account_id` is required, not optional. Fetching a topic unscoped returns
    *every* customer's agreement, and layering those together would apply one
    customer's negotiated terms to another — the cross-customer bleed the
    architecture treats as a correctness bug. Requiring the account makes that
    mistake impossible to make by omission.

    The `GENERAL` topic is included alongside the requested one because some
    parameters sit in sections whose headings carry no topic keyword — the
    SOP's "3. Approval and uncertainty" holds the manager-approval threshold
    for service credits. Extraction only recognises the patterns it knows, so
    widening the input set adds parameters without adding noise.

    Returns the raw layering set *and* the authority decision. The decision is
    what supplies override notes and citations; the layering set is what the
    term extractor consumes — it deliberately includes overridden-but-in-force
    clauses, because an agreement that states only a cap still relies on the
    SOP for everything else it does not mention.
    """
    evidence = get_evidence_by_topic(
        conn, topic.value, account_id=account_id, allowed_account_ids=allowed_account_ids
    )
    general = get_evidence_by_topic(
        conn,
        Topic.GENERAL.value,
        account_id=account_id,
        allowed_account_ids=allowed_account_ids,
    )

    seen: set[str] = set()
    combined: list[Evidence] = []
    for item in [*evidence, *general]:
        if item.chunk_id not in seen:
            seen.add(item.chunk_id)
            combined.append(item)

    # Authority is resolved over the topic evidence only: general-topic
    # preamble text is a parameter source, not a competing rule.
    decision = resolve_authority(evidence, account_id=account_id)
    return combined, decision


def hours_between(start: datetime, end: datetime) -> Decimal:
    """Signed hours from `start` to `end`, to two decimal places.

    Both operands are timezone-aware (Phase 2 stores ISO timestamps with an
    explicit offset), so this never silently compares across zones.
    """
    delta = (end - start).total_seconds() / 3600
    return Decimal(str(delta)).quantize(Decimal("0.01"))


def minutes_between(start: datetime, end: datetime) -> Decimal:
    delta = (end - start).total_seconds() / 60
    return Decimal(str(delta)).quantize(Decimal("0.01"))


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def citations(evidence: list[Evidence]) -> list[str]:
    """Stable, de-duplicated citation strings for a decision."""
    seen: list[str] = []
    for item in evidence:
        if item.citation not in seen:
            seen.append(item.citation)
    return seen
