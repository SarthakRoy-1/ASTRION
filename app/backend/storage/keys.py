"""Where a document's original file lives in object storage.

Ownership is part of the key, so a stray listing or a misconfigured policy can
never blur two owners together:

    workspaces/{org_id}/documents/{document_id}/{sha256}.pdf
    system/documents/{document_id}/{sha256}.pdf

The checksum is in the name. A file is therefore immutable at its key (the same
bytes always land in the same place, and different bytes never overwrite them),
which is what lets replacement be "write the new object, commit the database,
then remove the old one" without a window in which the row points at nothing.

Every component is validated against a strict pattern rather than sanitised: an
id that does not fit is refused, so nothing an upload can name -- a filename, a
document id derived from one -- reaches a path as a separator, a `..` or an
absolute path.
"""

from __future__ import annotations

import re

from app.backend.storage.errors import InvalidKeyError

_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-/]{0,900}$")

WORKSPACE_PREFIX = "workspaces/"
SYSTEM_PREFIX = "system/"


def _segment(name: str, value: str) -> str:
    if not isinstance(value, str) or not _SEGMENT.match(value) or ".." in value:
        raise InvalidKeyError(f"{name} is not a valid storage path component")
    return value


def workspace_document_key(org_id: str, document_id: str, sha256: str) -> str:
    if not _SHA256.match(sha256 or ""):
        raise InvalidKeyError("checksum is not a lowercase sha256 hex digest")
    return (
        f"{WORKSPACE_PREFIX}{_segment('org_id', org_id)}/documents/"
        f"{_segment('document_id', document_id)}/{sha256}.pdf"
    )


def system_document_key(document_id: str, sha256: str) -> str:
    if not _SHA256.match(sha256 or ""):
        raise InvalidKeyError("checksum is not a lowercase sha256 hex digest")
    return f"{SYSTEM_PREFIX}documents/{_segment('document_id', document_id)}/{sha256}.pdf"


def document_key(org_id: str | None, document_id: str, sha256: str) -> str:
    """The key for a document owned by `org_id`, or a system document if None."""
    if org_id is None:
        return system_document_key(document_id, sha256)
    return workspace_document_key(org_id, document_id, sha256)


def validate_key(key: str) -> str:
    """Refuse any key that is not plainly inside the store.

    Applied by every store on every call, whatever built the key, so a caller
    that constructs one by hand is held to the same rules as the builders above.
    """
    if not isinstance(key, str) or not _KEY.match(key):
        raise InvalidKeyError("storage key has characters that are not allowed")
    parts = key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise InvalidKeyError("storage key has an empty or relative path segment")
    return key
