"""Document management endpoints.

Reading is scoped exactly as retrieval is: a caller sees general documents and
the customer-specific documents of accounts in their scope, and nothing else.

Managing documents carries a constraint reading does not, because the
`documents` table is shared by every workspace. A *general* document — one
that names no `Account:` — is read by every tenant's agent, and a general
document with `Status: CURRENT` titled as a support policy would outrank the
supplied support policy for all of them. So:

- **Uploads must be customer-specific, and inside the caller's scope.** An
  uploaded agreement for an account the caller's workspace owns affects that
  workspace only, because account ownership is exclusive. General documents
  come from the supplied source pack and nowhere else.
- **The supplied source pack cannot be deleted through the API.** It is the
  authority every workspace's answers rest on.
- **Management requires a real workspace session.** The demo identity header
  carries no permissions to check, and a customer persona must not be able to
  delete a policy.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from pathlib import Path

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
from app.backend.ingestion.safety import UnsafeFileError, validate_upload
from app.backend.retrieval.authority import UnknownAuthorityError
from app.backend.retrieval.extraction import DocumentIngestionError, extract_document
from app.backend.services import documents
from app.backend.services.document_ingestion import ingest_single_document
from scripts.inspect_sources import PDF_FILES
from scripts.verify_source_pack import sha256_of

logger = logging.getLogger("astrion.documents")

router = APIRouter(prefix="/api/documents", tags=["documents"])

#: The supplied source pack. Every workspace's answers rest on these, so no
#: workspace may remove one.
CANONICAL_SOURCE_FILES = frozenset(PDF_FILES)


def _settings(request: Request) -> Settings:
    return request.app.state.settings


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


def _require_in_scope(caller: AuthenticatedCaller, account_id: str | None) -> None:
    """A managed document must belong to an account this workspace owns."""
    if caller.allowed_account_ids is None:
        return
    if account_id is None:
        raise AuthorizationError(
            "General documents apply to every workspace and come only from the "
            "supplied source pack. An uploaded document must state an Account: "
            "that belongs to this workspace."
        )
    if account_id not in caller.allowed_account_ids:
        raise AuthorizationError(
            "Cannot manage a document for an account outside your scope."
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


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("could not remove upload %s", path.name)


@router.get("", response_model=DocumentListResponse)
def list_documents(request: Request, conn: sqlite3.Connection = DbDep) -> DocumentListResponse:
    caller = _require(request, conn, Permission.READ_DOCUMENTS)

    docs = documents.list_documents(
        conn, allowed_account_ids=caller.allowed_account_ids
    )
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

    doc = documents.get_document(
        conn, document_id, allowed_account_ids=caller.allowed_account_ids
    )
    if not doc:
        raise NotFoundError(f"Document {document_id} not found")
    return DocumentMetadataResponse.model_validate(doc, from_attributes=True)


@router.get("/{document_id}/chunks", response_model=DocumentChunksResponse)
def get_document_chunks(request: Request, document_id: str, conn: sqlite3.Connection = DbDep) -> DocumentChunksResponse:
    caller = _require(request, conn, Permission.READ_DOCUMENTS)

    chunks = documents.get_document_chunks(
        conn, document_id, allowed_account_ids=caller.allowed_account_ids
    )
    if not chunks:
        # Differentiate between empty chunks and not found document
        doc = documents.get_document(
            conn, document_id, allowed_account_ids=caller.allowed_account_ids
        )
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

    try:
        content_bytes = file.file.read()
        validate_upload(
            content_bytes,
            filename=file.filename,
            declared_content_type=file.content_type,
        )
    except ValueError as e:
        raise InvalidRequestError(str(e))

    uploads_dir = _settings(request).uploads_dir
    uploads_dir.mkdir(parents=True, exist_ok=True)

    safe_filename = Path(file.filename or "upload.pdf").name
    # Prepend uuid to prevent name collision and overwrites
    dest_path = uploads_dir / f"{uuid.uuid4().hex}_{safe_filename}"
    dest_path.write_bytes(content_bytes)

    try:
        extracted = extract_document(dest_path, source_sha256=sha256_of(dest_path))
        _require_in_scope(caller, extracted.document.account_id)
        result = ingest_single_document(conn, extracted, str(uploads_dir))
    except AuthorizationError:
        _remove(dest_path)
        raise
    except DocumentIngestionError as exc:
        _remove(dest_path)
        logger.warning("document upload rejected: %s", exc)
        raise InvalidRequestError(
            f"Document ingestion failed: {_public_ingestion_message(exc)}"
        ) from exc
    except Exception as exc:
        _remove(dest_path)
        logger.exception("document upload failed")
        raise InvalidRequestError("Document ingestion failed.") from exc

    return {"ok": True, "counts": result}


@router.delete("/{document_id}")
def delete_document(request: Request, document_id: str, conn: sqlite3.Connection = DbDep) -> dict:
    caller = _require_management(request, conn)

    doc = documents.get_document(
        conn, document_id, allowed_account_ids=caller.allowed_account_ids
    )
    if not doc:
        raise NotFoundError(f"Document {document_id} not found")
    if doc.source_file in CANONICAL_SOURCE_FILES:
        raise AuthorizationError(
            "Documents from the supplied source pack cannot be deleted."
        )
    _require_in_scope(caller, doc.account_id)

    deleted = documents.delete_document(
        conn, document_id, allowed_account_ids=caller.allowed_account_ids
    )
    if not deleted:
        raise NotFoundError(f"Document {document_id} not found")

    _remove(_settings(request).uploads_dir / Path(doc.source_file).name)
    return {"ok": True}


@router.post("/reindex")
def reindex_documents(request: Request, conn: sqlite3.Connection = DbDep) -> dict:
    """Re-extract the uploaded documents that belong to this workspace.

    Per file rather than a wholesale reload of the uploads directory: that
    directory holds every workspace's uploads, and one workspace's reindex must
    neither rewrite another's documents nor be stopped by another's file.
    """
    caller = _require_management(request, conn)
    uploads_dir = _settings(request).uploads_dir

    counts = {"documents": 0, "chunks": 0, "skipped": 0}
    if not uploads_dir.exists():
        return {"ok": True, "counts": counts}

    for path in sorted(uploads_dir.glob("*.pdf")):
        if not path.is_file():
            continue
        try:
            extracted = extract_document(path, source_sha256=sha256_of(path))
        except DocumentIngestionError as exc:
            logger.warning("reindex skipped %s: %s", path.name, exc)
            counts["skipped"] += 1
            continue
        account_id = extracted.document.account_id
        if caller.allowed_account_ids is not None and (
            account_id is None or account_id not in caller.allowed_account_ids
        ):
            continue
        result = ingest_single_document(conn, extracted, str(uploads_dir))
        counts["documents"] += result["documents"]
        counts["chunks"] += result["chunks"]

    return {"ok": True, "counts": counts}
