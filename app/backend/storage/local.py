"""A store on the local filesystem: for tests and local development.

Nothing in production may depend on this surviving a restart -- on Render the
disk is ephemeral -- so `Settings.validate_persistence` refuses it there. It
implements exactly the same contract as the S3-compatible store, which is what
lets the whole upload lifecycle be tested without a network.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from app.backend.storage.errors import ObjectNotFound, StorageError
from app.backend.storage.keys import validate_key
from app.backend.storage.store import ObjectInfo, StoredObject


_LONG_PATH_PREFIX = "\\\\?\\"  # the Windows extended-length prefix: \\?\


def _fs(path: Path) -> Path:
    """The path as the filesystem should see it.

    Windows caps ordinary paths at 260 characters, and a key that encodes an
    owner, a document id and a checksum under a deep base directory can pass
    that; the extended-length prefix lifts the cap. A no-op everywhere else.
    """
    if os.name == "nt":
        text = str(path)
        if not text.startswith(_LONG_PATH_PREFIX):
            return Path(_LONG_PATH_PREFIX + text)
    return path


class LocalDocumentStore:
    backend = "local"

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        validate_key(key)
        root = self.root.resolve()
        path = (root / key).resolve()
        # Belt and braces: `validate_key` already refuses `..`, but the check
        # that matters is where the path actually lands.
        if root != path and root not in path.parents:
            raise StorageError("storage key resolves outside the store")
        return _fs(path)

    def put(self, key: str, data: bytes, *, content_type: str = "application/pdf") -> StoredObject:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a sibling temp file and rename, so a reader never sees half of one.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".put-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return StoredObject(key=key, size=len(data), sha256=hashlib.sha256(data).hexdigest())

    def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise ObjectNotFound(f"no object at {key}") from None

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        path = self._path(key)
        try:
            path.unlink()
        except FileNotFoundError:
            return  # already gone is the outcome that was asked for
        # Tidy empty parents up to the root, so deletes do not litter directories.
        parent = path.parent
        root = _fs(self.root.resolve())
        while parent != root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def list(self, prefix: str = "") -> Iterator[ObjectInfo]:
        base = _fs(self.root.resolve())
        if not base.exists():
            return
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.name.startswith(".put-"):
                continue
            key = path.relative_to(base).as_posix()
            if not key.startswith(prefix):
                continue
            stat = path.stat()
            yield ObjectInfo(
                key=key,
                size=stat.st_size,
                last_modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            )
