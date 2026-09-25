"""Object storage for documents' original files.

PostgreSQL holds a document's metadata and extracted text; the original file is
an object in a store, found through `documents.storage_key`. The store is an
abstraction with two implementations -- local disk (tests, development) and an
S3-compatible endpoint (Supabase Storage in production) -- chosen by
configuration, so no business logic names a vendor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.backend.storage.errors import (
    ChecksumMismatch,
    InvalidKeyError,
    ObjectNotFound,
    StorageError,
)
from app.backend.storage.keys import document_key, system_document_key, workspace_document_key
from app.backend.storage.local import LocalDocumentStore
from app.backend.storage.store import DocumentStore, ObjectInfo, StoredObject

if TYPE_CHECKING:  # pragma: no cover
    from app.backend.core.config import Settings


def open_store(settings: "Settings") -> DocumentStore:
    """The store these settings describe."""
    if settings.storage_backend == "s3":
        from app.backend.storage.s3 import S3DocumentStore

        return S3DocumentStore(
            bucket=settings.storage_bucket or "",
            endpoint_url=settings.storage_endpoint_url,
            region=settings.storage_region,
            access_key_id=settings.storage_access_key_id or "",
            secret_access_key=settings.storage_secret_access_key or "",
            key_prefix=settings.storage_key_prefix,
        )
    return LocalDocumentStore(settings.storage_local_dir or settings.uploads_dir)


__all__ = [
    "ChecksumMismatch",
    "DocumentStore",
    "InvalidKeyError",
    "LocalDocumentStore",
    "ObjectInfo",
    "ObjectNotFound",
    "StorageError",
    "StoredObject",
    "document_key",
    "open_store",
    "system_document_key",
    "workspace_document_key",
]
