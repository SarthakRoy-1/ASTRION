"""Storage exceptions. Messages never carry credentials or endpoint secrets."""

from __future__ import annotations


class StorageError(RuntimeError):
    """The object store could not do what was asked."""


class ObjectNotFound(StorageError):
    """No object is stored at that key."""


class InvalidKeyError(StorageError, ValueError):
    """The key is not one this application would ever build."""


class ChecksumMismatch(StorageError):
    """Stored bytes do not match the checksum recorded for them."""
