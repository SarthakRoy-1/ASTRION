"""The document-store contract.

Business logic talks to this, never to a vendor SDK. Four operations are needed
by the product -- put, get, exists, delete -- plus `list`, which only the
orphan reconciler uses. A store keeps bytes under keys and nothing else: what a
document *means* (its owner, its extracted text, its checksum) lives in the
database, and the two are tied together by `documents.storage_key`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class StoredObject:
    """What `put` reports back: where the object went and what was written."""

    key: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ObjectInfo:
    """One object in a listing."""

    key: str
    size: int
    last_modified: datetime | None


class DocumentStore(Protocol):
    backend: str

    def put(self, key: str, data: bytes, *, content_type: str = "application/pdf") -> StoredObject:
        """Write `data` at `key`, replacing any object already there."""

    def get(self, key: str) -> bytes:
        """The object's bytes. Raises `ObjectNotFound` if there is none."""

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None:
        """Remove the object. Removing one that is already gone is not an error."""

    def list(self, prefix: str = "") -> Iterator[ObjectInfo]:
        """Every object whose key starts with `prefix`."""
