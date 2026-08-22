"""Models for the document/evidence layer (Phase 3).

Read-side representations of the `documents` and `document_chunks` tables
created by app/backend/services/database.py and populated by
scripts/ingest_documents.py, plus the agent-facing `Evidence` object and the
result of deterministic authority resolution.

As in app/backend/models/records.py, these are not an ORM — nothing here
writes to the database. Enum values are stored as TEXT/INTEGER in SQLite so
the database stays readable without application code.

Nothing in this module encodes a business rule. Authority *ranking* is
represented here (which source outranks which); the actual SLA / cancellation
/ service-credit arithmetic that consumes those rankings is Phase 4.
"""

from __future__ import annotations

from datetime import date
from enum import IntEnum, StrEnum

from pydantic import BaseModel, ConfigDict


class DocumentType(StrEnum):
    """Derived from the document's own title/preamble, never from a guess."""

    CUSTOMER_AGREEMENT = "customer_agreement"
    SUPPORT_POLICY = "support_policy"
    SOP = "sop"
    PRODUCT_DOCUMENTATION = "product_documentation"


class DocumentStatus(StrEnum):
    """The `Status:` value stated in the document preamble, normalised to its
    leading keyword (e.g. "DEPRECATED - DO NOT USE FOR CURRENT REQUESTS"
    normalises to DEPRECATED). The full original string is kept alongside."""

    CURRENT = "CURRENT"
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"


class AuthorityTier(IntEnum):
    """Source precedence. LOWER number = HIGHER authority.

    Mirrors the hierarchy in docs/architecture.md section 2 and the
    precedence clause stated inside 01_Support_Policy_v3_CURRENT.pdf itself
    ("use the signed customer agreement first, then the current support
    policy, then current product documentation").

    Authority is deliberately independent of retrieval relevance: a chunk can
    score highly on text similarity and still be non-authoritative.
    """

    CUSTOMER_AGREEMENT = 1
    CURRENT_SUPPORT_POLICY = 2
    CURRENT_OPERATIONAL_DOC = 3  # current SOP / current product documentation
    NON_AUTHORITATIVE = 4  # deprecated or superseded — context only, never governing


class Topic(StrEnum):
    """Subject-matter domain a chunk speaks to, derived from its section
    heading. Used so that "current SOP / product documentation according to
    the subject matter" is decidable in code: an override only applies
    between sources discussing the *same* topic.
    """

    SUPPORT_RESPONSE = "support_response"  # severity, response targets, escalation
    CANCELLATION = "cancellation"
    SERVICE_CREDIT = "service_credit"
    PRODUCT_KNOWN_ISSUES = "product_known_issues"
    GENERAL = "general"


class Document(BaseModel):
    """One source PDF, with metadata read out of its own preamble."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    source_file: str
    source_sha256: str
    title: str
    document_type: DocumentType
    status: DocumentStatus
    status_raw: str
    is_current: bool
    is_deprecated: bool
    is_authoritative: bool
    authority_tier: AuthorityTier

    # Customer scope. None for general (non customer-specific) documents.
    account_id: str | None
    customer_name: str | None
    plan: str | None

    effective_date_raw: str | None
    effective_date: date | None
    updated_date_raw: str | None
    updated_date: date | None
    term_raw: str | None
    term_start: date | None
    term_end: date | None
    supersedes: str | None
    superseded_by: str | None

    page_count: int


class DocumentChunk(BaseModel):
    """A section-aware slice of one page of one document."""

    model_config = ConfigDict(frozen=True)

    chunk_id: str
    document_id: str
    chunk_ordinal: int
    page_number: int
    section_number: str | None
    section_title: str | None
    subsection_title: str | None
    section_path: str | None
    topic: Topic
    text: str
    char_count: int
    word_count: int
    # Offsets into the normalised text of `page_number`, so a chunk's text can
    # be re-verified against the PDF it came from. See
    # app/backend/retrieval/extraction.py:extract_page_texts.
    page_char_start: int
    page_char_end: int


class Evidence(BaseModel):
    """What the retrieval layer hands back — a chunk plus everything needed to
    cite it and to judge its authority, flattened so a caller never has to
    traverse relationships to build a citation.

    This is the object a future agent tool serialises into a model prompt.
    """

    model_config = ConfigDict(frozen=True)

    # --- identity -----------------------------------------------------------
    chunk_id: str
    document_id: str

    # --- the retrieved text -------------------------------------------------
    text: str

    # --- provenance: where exactly this came from ---------------------------
    source_file: str
    source_sha256: str
    page_number: int
    section_number: str | None
    section_title: str | None
    subsection_title: str | None
    section_path: str | None
    page_char_start: int
    page_char_end: int

    # --- authority / status metadata ----------------------------------------
    document_title: str
    document_type: DocumentType
    status: DocumentStatus
    status_raw: str
    is_current: bool
    is_deprecated: bool
    is_authoritative: bool
    authority_tier: AuthorityTier
    topic: Topic
    effective_date: date | None
    updated_date: date | None
    supersedes: str | None
    superseded_by: str | None

    # --- account scope -------------------------------------------------------
    account_id: str | None
    customer_name: str | None

    # --- retrieval ------------------------------------------------------------
    score: float | None = None

    @property
    def citation(self) -> str:
        """Human-readable citation, e.g.
        "03_Cancellation_and_Service_Credit_SOP_v4.pdf p.1 §1. Order cancellation"."""
        base = f"{self.source_file} p.{self.page_number}"
        if self.section_path:
            return f"{base} §{self.section_path}"
        return base


class OverrideNote(BaseModel):
    """A deterministic record that one source outranks another on a topic.

    Emitted so an answer can say *why* a customer is treated differently,
    naming both sources — a product requirement, not a debugging aid.
    """

    model_config = ConfigDict(frozen=True)

    topic: Topic
    winning_chunk_id: str
    winning_document_id: str
    winning_source_file: str
    winning_authority_tier: AuthorityTier
    overridden_chunk_id: str
    overridden_document_id: str
    overridden_source_file: str
    overridden_authority_tier: AuthorityTier
    reason: str


class ConflictNote(BaseModel):
    """Two or more *different* documents at the same authority tier speaking to
    the same topic. Precedence cannot settle this, so it is surfaced for
    escalation rather than silently resolved by picking one."""

    model_config = ConfigDict(frozen=True)

    topic: Topic
    authority_tier: AuthorityTier
    chunk_ids: tuple[str, ...]
    document_ids: tuple[str, ...]
    reason: str


class AuthorityDecision(BaseModel):
    """Result of resolving a set of evidence into governing vs. contextual.

    `governing` holds the highest-authority evidence for each topic present.
    `contextual` holds everything else — outranked material and deprecated
    documents — which is still returned in full, because explaining that a
    rule changed requires quoting the rule that changed.
    """

    governing: list[Evidence]
    contextual: list[Evidence]
    overrides: list[OverrideNote]
    conflicts: list[ConflictNote]

    @property
    def has_unresolved_conflict(self) -> bool:
        return bool(self.conflicts)

    def governing_for(self, topic: Topic) -> list[Evidence]:
        """Governing evidence for one subject-matter topic.

        Topics resolve independently, so a search that touches several topics
        yields governing evidence for each. A caller answering a cancellation
        question wants `governing_for(Topic.CANCELLATION)`, not the whole
        governing list.
        """
        return [e for e in self.governing if e.topic is topic]
