"""Schemas for the document management API."""

from __future__ import annotations

from datetime import date
from pydantic import BaseModel, ConfigDict


class DocumentMetadataResponse(BaseModel):
    """Metadata for a single document."""
    model_config = ConfigDict(frozen=True)

    document_id: str
    #: True for platform knowledge that belongs to no workspace and that every
    #: workspace's assistant may cite; False for a workspace's own document. The
    #: owning workspace's id is deliberately not part of the response.
    is_system_document: bool = False
    source_file: str
    title: str
    document_type: str
    status: str
    status_raw: str
    is_current: bool
    is_deprecated: bool
    is_authoritative: bool
    authority_tier: int
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


class DocumentListResponse(BaseModel):
    """List of visible documents."""
    model_config = ConfigDict(frozen=True)

    documents: list[DocumentMetadataResponse]


class DocumentChunkResponse(BaseModel):
    """A single chunk of a document."""
    model_config = ConfigDict(frozen=True)

    chunk_id: str
    document_id: str
    page_number: int
    section_number: str | None
    section_title: str | None
    subsection_title: str | None
    section_path: str | None
    topic: str
    text: str


class DocumentChunksResponse(BaseModel):
    """List of chunks for a document."""
    model_config = ConfigDict(frozen=True)

    chunks: list[DocumentChunkResponse]


class IngestionStatusResponse(BaseModel):
    """Status of a document ingestion run."""
    model_config = ConfigDict(frozen=True)

    id: int
    started_at_utc: str
    finished_at_utc: str | None
    source_dir: str
    status: str
    document_count: int | None
    chunk_count: int | None
