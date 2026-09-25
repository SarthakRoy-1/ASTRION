"""Document management endpoints.

Reading is scoped exactly as retrieval is: a caller sees general documents and
the customer-specific documents of accounts in their scope, and nothing else.

Managing documents carries a constraint reading does not, because the
`documents` table is shared by every workspace. A *general* document — one
that names no `Account:` — is read by every tenant's agent, and a general
document with `Status: CURRENT` titled as a support policy would outrank the
supplied support policy for all of them. So:

- **An upload belongs to the workspace that made it.** It is stored with the
  caller's `org_id`, so it is visible to that workspace and to no other. If it
  names an `Account:`, that account must be one the workspace has. It never
  becomes a system document: those come only from the platform's own loader.
- **System documents cannot be deleted through the API.** They are the platform
  knowledge every workspace's answers rest on, and no workspace owns them.
- **Management requires a real workspace session.** The demo identity header
  carries no permissions to check, and a customer persona must not be able to
  delete a policy.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid

from fastapi import APIRouter, File, Request, UploadFile

from app.backend.api.authentication import AuthenticatedCaller, audit_denial, authenticate
from app.backend.api.dependencies import DbDep
from app.backend.api.document_schemas import (
    DocumentChunksResponse,
    DocumentListResponse,
    DocumentMetadataResponse,
    IngestionStatusResponse,
)
from app.backend.auth.permissions import Permission
from app.backend.core.config import Settings
from app.backend.core.errors import AuthorizationError, InvalidRequestError, NotFoundError
from app.backend.ingestion.safety import UnsafeFileError, sanitize_filename, validate_upload
from app.backend.retrieval.authority import UnknownAuthorityError
from app.backend.retrieval.extraction import DocumentIngestionError
from app.backend.services import documents
from app.backend.services.document_files import (
    delete_document_and_object,
    extract_from_bytes,
    read_verified,
    store_and_ingest,
)
from app.backend.services.records import get_account
from app.backend.storage import ChecksumMismatch, DocumentStore, ObjectNotFound, StorageError

logger = logging.getLogger("astrion.documents")

router = APIRouter(prefix="/api/documents", tags=["documents"])

def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _store(request: Request) -> DocumentStore:
    return request.app.state.store


def _caller(request: Request, conn: sqlite3.Connection) -> AuthenticatedCaller:
    return authenticate(request, conn, _settings(request))


def _require(
    request: Request, conn: sqlite3.Connection, permission: Permission
) -> AuthenticatedCaller:
    """Reads. Demo personas carry no permission set, so they are admitted and
    scoped by account exactly as every other read route scopes them."""
    caller = _caller(request, conn)
    if not caller.is_demo and not caller.has(permission):
        audit_denial(
            conn,
            caller,
            permission=permission,
            detail=f"lacks {permission.value}",
        )
        raise AuthorizationError(
            f"This action requires the {permission.value!r} permission."
        )
    return caller


def _require_management(
    request: Request, conn: sqlite3.Connection
) -> AuthenticatedCaller:
    """Writes. Never admitted on the demo identity header.

    The header path has no permission set to consult, so skipping the check
    for it — as reads do — would hand upload and delete to every persona,
    including the customer ones.
    """
    caller = _caller(request, conn)
    if caller.is_demo:
        raise AuthorizationError(
            "Document management requires a real workspace session. "
            "Set AUTH_MODE=session to use it."
        )
    if not caller.has(Permission.MANAGE_DOCUMENTS):
        audit_denial(
            conn,
            caller,
            permission=Permission.MANAGE_DOCUMENTS,
            detail=f"lacks {Permission.MANAGE_DOCUMENTS.value}",
        )
        raise AuthorizationError(
            f"This action requires the {Permission.MANAGE_DOCUMENTS.value!r} permission."
        )
    return caller


def _require_workspace(caller: AuthenticatedCaller) -> str:
    """The workspace a managed document belongs to. A caller with none has none."""
    if caller.org_id is None:
        raise AuthorizationError(
            "Create or join a workspace before managing documents."
        )
    return caller.org_id


def _require_account_in_workspace(
    conn: sqlite3.Connection, caller: AuthenticatedCaller, account_id: str | None
) -> None:
    """A document that names an account must name one this workspace has.

    A document that names none is the workspace's own general document.
    """
    if account_id is None:
        return
    if get_account(conn, account_id, scope=caller.scope()) is None:
        raise AuthorizationError(
            "That document names an account that is not in this workspace."
        )


def _public_ingestion_message(exc: DocumentIngestionError) -> str:
    """What an uploader may be told about a document that failed to ingest.

    Validation failures describe the document and are worth reading. A failure
    that wraps a lower-level library error can carry server paths, so it is
    reported generically and logged in full instead.
    """
    cause = exc.__cause__
    if cause is None or isinstance(cause, (UnknownAuthorityError, UnsafeFileError)):
        return str(exc)
    return "the file could not be read as a PDF."


@router.get("", response_model=DocumentListResponse)
def list_documents(request: Request, conn: sqlite3.Connection = DbDep) -> DocumentListResponse:
    caller = _require(request, conn, Permission.READ_DOCUMENTS)

    docs = documents.list_documents(conn, scope=caller.scope())
    return DocumentListResponse(documents=[
        DocumentMetadataResponse.model_validate(d, from_attributes=True)
        for d in docs
    ])


@router.get("/ingestion-status", response_model=IngestionStatusResponse)
def get_ingestion_status(request: Request, conn: sqlite3.Connection = DbDep) -> IngestionStatusResponse:
    _require(request, conn, Permission.READ_DOCUMENTS)

    status = documents.get_latest_document_ingestion_run(conn)
    if not status:
        raise NotFoundError("No ingestion runs found.")
    return IngestionStatusResponse(**status)


@router.get("/{document_id}", response_model=DocumentMetadataResponse)
def get_document(request: Request, document_id: str, conn: sqlite3.Connection = DbDep) -> DocumentMetadataResponse:
    caller = _require(request, conn, Permission.READ_DOCUMENTS)

    doc = documents.get_document(conn, document_id, scope=caller.scope())
    if not doc:
        raise NotFoundError(f"Document {document_id} not found")
    return DocumentMetadataResponse.model_validate(doc, from_attributes=True)


@router.get("/{document_id}/chunks", response_model=DocumentChunksResponse)
def get_document_chunks(request: Request, document_id: str, conn: sqlite3.Connection = DbDep) -> DocumentChunksResponse:
    caller = _require(request, conn, Permission.READ_DOCUMENTS)

    chunks = documents.get_document_chunks(conn, document_id, scope=caller.scope())
    if not chunks:
        # Differentiate between empty chunks and not found document
        doc = documents.get_document(conn, document_id, scope=caller.scope())
        if not doc:
            raise NotFoundError(f"Document {document_id} not found")

    return DocumentChunksResponse(chunks=[c.model_dump() for c in chunks])


@router.post("/upload")
def upload_document(
    request: Request,
    conn: sqlite3.Connection = DbDep,
    file: UploadFile = File(...),
) -> dict:
    caller = _require_management(request, conn)
    org_id = _require_workspace(caller)

    try:
        content_bytes = file.file.read()
        validate_upload(
            content_bytes,
            filename=file.filename,
            declared_content_type=file.content_type,
        )
    except ValueError as e:
        raise InvalidRequestError(str(e))

    # Rebuilt from safe characters, never cleaned: the name is untrusted input.
    safe_filename = sanitize_filename(file.filename or "upload.pdf", fallback="upload.pdf")
    # The prefix keeps two uploads of one filename from colliding; it becomes
    # part of the document's id.
    source_file = f"{uuid.uuid4().hex}_{safe_filename}"

    try:
        extracted = extract_from_bytes(
            content_bytes, source_file=source_file, fallback_name=safe_filename
        )
        _require_account_in_workspace(conn, caller, extracted.document.account_id)
        result = store_and_ingest(
            conn,
            _store(request),
            org_id=org_id,
            content=content_bytes,
            original_filename=safe_filename,
            content_type="application/pdf",
            extracted=extracted,
            source_dir="upload",
        )
    except AuthorizationError:
        raise
    except DocumentIngestionError as exc:
        logger.warning("document upload rejected: %s", exc)
        raise InvalidRequestError(
            f"Document ingestion failed: {_public_ingestion_message(exc)}"
        ) from exc
    except StorageError as exc:
        # The file could not be kept. Nothing was recorded (see document_files).
        logger.error("document upload could not be stored: %s", exc)
        raise InvalidRequestError(
            "The document could not be stored right now. Try again."
        ) from exc
    except Exception as exc:
        logger.exception("document upload failed")
        raise InvalidRequestError("Document ingestion failed.") from exc

    return {"ok": True, "counts": result}


@router.delete("/{document_id}")
def delete_document(request: Request, document_id: str, conn: sqlite3.Connection = DbDep) -> dict:
    caller = _require_management(request, conn)
    _require_workspace(caller)

    doc = documents.get_document(conn, document_id, scope=caller.scope())
    if not doc:
        raise NotFoundError(f"Document {document_id} not found")
    if doc.org_id is None:
        raise AuthorizationError("System documents cannot be deleted.")

    # The row first, the object only after that has committed; an object that
    # cannot be removed is recorded for reconciliation rather than failing this.
    deleted = delete_document_and_object(
        conn,
        _store(request),
        delete_row=lambda: documents.delete_document(conn, document_id, scope=caller.scope()),
        storage_key=doc.storage_key,
        org_id=doc.org_id,
    )
    if not deleted:
        raise NotFoundError(f"Document {document_id} not found")
    return {"ok": True}


@router.post("/reindex")
def reindex_documents(request: Request, conn: sqlite3.Connection = DbDep) -> dict:
    """Re-extract this workspace's own documents from their stored originals.

    Driven by the workspace's own document rows, so another workspace's files are
    never opened, and each original is read back from the store and checked
    against its recorded checksum before it is trusted.
    """
    caller = _require_management(request, conn)
    org_id = _require_workspace(caller)
    store = _store(request)

    counts = {"documents": 0, "chunks": 0, "skipped": 0}
    for doc in documents.list_documents(conn, scope=caller.scope()):
        if doc.org_id != org_id:
            continue
        if not doc.storage_key:
            counts["skipped"] += 1  # no original was ever stored for this one
            continue
        try:
            content = read_verified(store, doc.storage_key, doc.source_sha256)
            extracted = extract_from_bytes(
                content,
                source_file=doc.source_file,
                expected_sha256=doc.source_sha256,
                fallback_name=doc.original_filename or doc.source_file,
            )
        except (ObjectNotFound, ChecksumMismatch, StorageError, DocumentIngestionError) as exc:
            logger.warning("reindex skipped %s: %s", doc.document_id, exc)
            counts["skipped"] += 1
            continue
        result = store_and_ingest(
            conn,
            store,
            org_id=org_id,
            content=content,
            original_filename=doc.original_filename or doc.source_file,
            content_type=doc.content_type or "application/pdf",
            extracted=extracted,
            source_dir="upload",
        )
        counts["documents"] += result["documents"]
        counts["chunks"] += result["chunks"]

    return {"ok": True, "counts": counts}
