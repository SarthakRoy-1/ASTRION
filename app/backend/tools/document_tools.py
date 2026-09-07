"""Tool A — document search and evidence retrieval.

A thin wrapper over Phase 3. It adds no ranking, no re-ordering, and no
authority logic of its own: `search_and_resolve` already returns evidence with
precedence resolved, and duplicating any of that here would create a second
competing authority implementation.

What this layer contributes is the agent-facing shape — a serialisable summary
plus the typed `Evidence` list — and the guarantee that scoping comes from the
execution context.
"""

from __future__ import annotations

import sqlite3

from app.backend.models.agent import AgentContext, ToolResult, ToolStatus
from app.backend.retrieval.search import get_document_evidence, search_and_resolve
from app.backend.tools.base import ToolSpec, require_str

SEARCH_DOCUMENTS = "search_documents"
GET_DOCUMENT_EVIDENCE = "get_document_evidence"


def _evidence_summary(item, *, governing: bool) -> dict:
    return {
        "chunk_id": item.chunk_id,
        "document_id": item.document_id,
        "source_file": item.source_file,
        "page": item.page_number,
        "section": item.section_path,
        "citation": item.citation,
        "text": item.text,
        "authority_tier": int(item.authority_tier),
        "status": item.status.value,
        "is_authoritative": item.is_authoritative,
        "is_deprecated": item.is_deprecated,
        "topic": item.topic.value,
        "account_id": item.account_id,
        "governing": governing,
    }


def _search_documents(
    conn: sqlite3.Connection, context: AgentContext, arguments: dict
) -> ToolResult:
    query, error = require_str(arguments, "query")
    if error is not None:
        return error

    account_id = arguments.get("account_id")
    if account_id is not None and not isinstance(account_id, str):
        return ToolResult(
            status=ToolStatus.INVALID_INPUT, message="account_id must be a string"
        )

    limit = arguments.get("limit", 8)
    if not isinstance(limit, int) or not 1 <= limit <= 25:
        return ToolResult(
            status=ToolStatus.INVALID_INPUT, message="limit must be an integer 1-25"
        )

    decision = search_and_resolve(
        conn,
        query,
        account_id=account_id,
        allowed_account_ids=context.scope(),
        limit=limit,
    )

    evidence = [*decision.governing, *decision.contextual]
    if not evidence:
        return ToolResult(
            status=ToolStatus.NO_EVIDENCE,
            message=f"no document evidence matched {query!r} within the caller's scope",
            data={"query": query, "account_id": account_id},
        )

    governing_ids = {item.chunk_id for item in decision.governing}
    return ToolResult(
        status=ToolStatus.OK,
        evidence=evidence,
        data={
            "query": query,
            "account_id": account_id,
            "governing": [
                _evidence_summary(item, governing=True) for item in decision.governing
            ],
            "contextual": [
                _evidence_summary(item, governing=item.chunk_id in governing_ids)
                for item in decision.contextual
            ],
            "overrides": [note.reason for note in decision.overrides],
            "conflicts": [note.reason for note in decision.conflicts],
            "has_unresolved_conflict": decision.has_unresolved_conflict,
        },
    )


def _get_document_evidence(
    conn: sqlite3.Connection, context: AgentContext, arguments: dict
) -> ToolResult:
    chunk_ids = arguments.get("chunk_ids")
    document_id = arguments.get("document_id")

    if (chunk_ids is None) == (document_id is None):
        return ToolResult(
            status=ToolStatus.INVALID_INPUT,
            message="provide exactly one of chunk_ids or document_id",
        )
    if chunk_ids is not None and (
        not isinstance(chunk_ids, list) or not all(isinstance(c, str) for c in chunk_ids)
    ):
        return ToolResult(
            status=ToolStatus.INVALID_INPUT, message="chunk_ids must be a list of strings"
        )

    evidence = get_document_evidence(
        conn,
        chunk_ids=chunk_ids,
        document_id=document_id,
        account_id=arguments.get("account_id"),
        allowed_account_ids=context.scope(),
    )
    if not evidence:
        return ToolResult(
            status=ToolStatus.NOT_FOUND,
            message="no evidence found for that selector within the caller's scope",
        )
    return ToolResult(
        status=ToolStatus.OK,
        evidence=evidence,
        data={"evidence": [_evidence_summary(e, governing=False) for e in evidence]},
    )


SEARCH_DOCUMENTS_SPEC = ToolSpec(
    name=SEARCH_DOCUMENTS,
    description=(
        "Search ASTRION policies, SOPs, product documentation and customer "
        "agreements. Returns evidence with source file, page, section and authority "
        "metadata, already split into governing versus contextual by source "
        "precedence. Pass account_id when the question concerns a specific customer "
        "so that customer's agreement can take precedence."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural-language search text."},
            "account_id": {
                "type": "string",
                "description": "Account the question is about, e.g. ACCT-001. Optional.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 8},
        },
        "required": ["query"],
    },
    handler=_search_documents,
)

GET_DOCUMENT_EVIDENCE_SPEC = ToolSpec(
    name=GET_DOCUMENT_EVIDENCE,
    description=(
        "Fetch document evidence by identity rather than relevance: either specific "
        "chunk_ids previously cited, or every chunk of one document_id."
    ),
    parameters={
        "type": "object",
        "properties": {
            "chunk_ids": {"type": "array", "items": {"type": "string"}},
            "document_id": {"type": "string"},
            "account_id": {"type": "string"},
        },
    },
    handler=_get_document_evidence,
)
